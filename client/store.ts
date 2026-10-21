import { useSyncExternalStore } from "use-sync-external-store/shim";

import { BrowserDatabase, BrowserResource, createBrowserDatabase, INTERNAL_ROW_ID } from "./dataframeSqlite";
import type { QueryOptions } from "./dataframe";
import { decodeDataFrameRows } from "./dataframeValueConversions";
import { ChangeInfo, JS, JSONObject, LocalTransaction, NODE, sameJSON, StoreDoc, StoreNode, StoreOp } from "./storeTree";

export type { JSONArray, JSONObject, JSONValue } from "./storeTree";
export type Store<T = any> = any;

// Generate Store operation and origin IDs on HTTP pages where randomUUID is unavailable
export function randomUUID(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

const nodeOf = (proxy: any, caller: string): StoreNode => {
  const node: StoreNode | undefined = proxy?.[NODE];
  if (!node) throw new Error(`${caller} expects a Store value`);
  return node;
};

// pret.store rebuild_doc calls this with the values and revision Python sent
export function createStoreDoc(state: JSONObject, revision: number): StoreDoc {
  return new StoreDoc(state, revision);
}

// pret.store snapshot calls this to read the current plain mapping or array
export function snapshot(proxy: any): any {
  return proxy?.[JS] || proxy;
}

// pret.store subscribe calls this, callback receives the operations under the value and their change
export function subscribe(proxy: any, callback: (ops: StoreOp[], info: ChangeInfo) => void): () => void {
  const node = nodeOf(proxy, "subscribe");
  return node.doc.subscribe(node, callback);
}

// pret.hooks use_store_snapshot calls this React hook to subscribe to a Store
export function useSnapshot<T = any>(proxy: any): T {
  const node = nodeOf(proxy, "useSnapshot");
  // Suspend widget mounting until selection effects can write to the initialized Store
  (node.doc.__pret_controller__ as StoreController | null)?.suspendUntilReady();
  return useSyncExternalStore(
    (callback) => node.doc.listen(callback),
    () => node.snapshot() as T
  );
}

// pret.store transact calls this when entering its browser context and invokes the returned function on exit.
// Compiled Python calls the exit function after the block, with the error if it raised, and again in finally.
export const beginTransaction = (proxy: any): [LocalTransaction, (errorType?: unknown, error?: unknown) => void] => {
  const doc = nodeOf(proxy, "beginTransaction").doc;
  const transaction = doc.begin();
  // A nested transaction joins the open one, and only makes it fail on error
  if (!transaction) {
    return [doc.transaction, (errorType, error) => {
      if (errorType || error) doc.abort(error || errorType);
    }];
  }
  let ended = false;
  return [transaction, (errorType, error) => {
    if (ended) return;
    ended = true;
    doc.end(transaction, errorType || error ? error || errorType : undefined);
  }];
};

type Op = {
  request_id: string;
  origin: string;
  kind: "edit" | "undo" | "redo";
  // Revision shown when the op started, Python moves its operations over the commits saved since
  base_revision: number;
  ops: StoreOp[];
  edits: any[];
  // Inverses of ops while their values are only local, replayed backwards when Python saved something else
  inverse?: StoreOp[];
  // Paged Stores show these values in loaded rows until Python replies, before restores them on rejection
  optimistic?: Record<string, Map<unknown, { before: Record<string, unknown>; values: Record<string, unknown> }>>;
};
type CommitRemoteOp = (op: Omit<Op, "optimistic" | "inverse">) => Promise<any>;
type GetRemoteDataChanges = (revision?: number, watched?: Record<string, unknown[]>) => Promise<any>;
type HistorySnapshot = {
  canUndo: boolean;
  canRedo: boolean;
  pending: boolean;
  connected: boolean;
  error: string | null;
  revision: number;
};
const controllers = new Map<string, StoreController>();
const pendingBindings = new Map<string, any[]>();

export const errorText = (error: any): string => String(error?.message ?? error);

// Handle edits for one Store in one browser page, attachStore reuses it across widgets showing that Store.
//
// A local transaction and the table edits made inside it form one op. Connected Stores send the op to
// StoreDoc.apply_browser_edit, which decides conflicts, saves it and keeps this page's undo history, then the page
// fetches the saved commits with StoreDoc.browser_data. Only one op is pending at a time.
//
// | Store          | Tables                     | While the op is pending           | Undo       |
// | -------------- | -------------------------- | --------------------------------- | ---------- |
// | browser only   | BrowserDatabase            | written locally, kept at once     | unavailable|
// | paged          | Python queries             | optimistic values in loaded rows  | Python     |
// | full           | BrowserDatabase copy       | written in a savepoint            | Python     |
//
// phase follows the op: loading, then ready, editing while a local transaction is open, pending once sent,
// uncertain when its reply was lost (resent on reconnection), back to ready when Python answers
type Phase = "loading" | "ready" | "editing" | "pending" | "uncertain";

class StoreController {
  readonly ready: Promise<void>;
  private database: BrowserDatabase | null = null;
  private resources: Record<string, any> = {};
  private identityAliases = new Map<string, Map<number, number>>();
  private phase: Phase = "loading";
  // The op being edited, sent or resent, set while phase is editing, pending or uncertain
  private activeOp: Op | null = null;
  private catchUpPromise: Promise<void> | null = null;
  // A Python notification arrived during an op, fetch its changes once the op finishes
  private catchUpRequested = false;
  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private connected = true;
  private error: string | null = null;
  private revision = 0;
  private canUndoOp = false;
  private canRedoOp = false;
  private listeners = new Set<() => void>();
  private opPromise: Promise<any> = Promise.resolve(null);
  private historySnapshot: HistorySnapshot;
  // pret.store rebuild_doc reports edits refused while disconnected to the connection status
  onRejectedWrite: ((reason: string) => void) | null = null;

  constructor(
    readonly doc: StoreDoc,
    resources: Record<string, any>,
    // StoreDoc.apply_browser_edit saves an edit, undo or redo op, null for browser only Stores
    private commitRemoteOp: CommitRemoteOp | null,
    readonly origin: string,
    // StoreDoc.browser_data returns the values and tables (no revision), then the commits since the provided revision
    readonly getRemoteDataChanges: GetRemoteDataChanges | null = null,
    readonly dataframeSync: "full" | "paged" = "full"
  ) {
    doc.__pret_controller__ = this;
    this.revision = doc.revision;
    this.bindResources(resources);
    this.notifyHistory();
    this.ready = (async () => {
      // View decoding attaches the communication handler before synchronization starts
      await Promise.resolve();
      if (getRemoteDataChanges) {
        // Timing logs of the initial synchronization, read when diagnosing slow view openings
        const start = performance.now();
        const timing = (stage: string) =>
          console.info("PRET timing:", stage, { ms: Math.round(performance.now() - start), at: Math.round(performance.now()) });
        let initial = null;
        try {
          initial = await getRemoteDataChanges(undefined, dataframeSync === "paged" ? {} : undefined);
        } catch (error) {
          // Without Python, e.g. on a static page, a paged Store shows the values sent with the page and refuses
          // edits until setConnected fetches the later commits. A full copy has no tables to show
          if (dataframeSync === "full") throw error;
          this.connected = false;
          this.error = errorText(error);
        }
        if (initial) {
          timing("Store data received");
          if (dataframeSync === "full") {
            this.database = await createBrowserDatabase(this.readTableBatches(initial));
            timing("browser table copies built");
          }
          this.setRevision(initial.revision);
          doc.reconcile(initial.state);
          this.bindResources(resources);
        }
      } else {
        const inputs: BrowserResource[] = Object.values(this.resources).map((resource) => resource.offlineRows);
        this.database = await createBrowserDatabase(inputs);
      }
      this.phase = "ready";
      this.notifyHistory();
      this.startPolling();
      if (getRemoteDataChanges && this.connected) await this.catchUpRemoteChanges();
    })().catch((error) => {
      this.error = errorText(error);
      this.notifyHistory();
      throw error;
    });
    void this.ready.catch(() => {});
  }

  // Let useSnapshot mount widgets after initialization or report the loading error to React
  suspendUntilReady(): void {
    if (this.phase === "loading") {
      if (this.error) throw new Error(this.error);
      throw this.ready;
    }
  }

  // Attach DataFrame methods to this controller so widgets share its database and ops
  bindResources(resources: Record<string, any>): void {
    for (const [id, resource] of Object.entries(resources)) {
      const alreadyBound = resource.controller === this;
      this.resources[id] = resource;
      resource.controller = this;
      const local =
        !this.commitRemoteOp || (this.dataframeSync === "full" && resource.localCopy) || this.database?.hasResource(id);
      if (local) {
        // Queries wait for the local tables and report the revision they show
        resource.describe = async () => {
          await this.ready;
          return this.database.describe(id);
        };
        resource.query = async (options: QueryOptions) => {
          await this.ready;
          return this.database.query(id, options);
        };
      }
      // Tables edited through a connected Store report the revision of the rows they last received
      const connected = this.getRemoteDataChanges && resource.transactional !== false;
      if (local || connected) {
        resource.subscribeChanges = this.subscribe;
        resource.get_changes_after_revision = async () => ({ revision: connected ? resource.revision : this.revision });
      }
      // Another widget can attach the same resource, wrap its original edit callback only once
      if (!alreadyBound) {
        resource.resolve_identity = (value) => this.identityAliases.get(id)?.get(Number(value)) ?? value;
        resource.pendingValues = (row) => {
          const pending = this.activeOp?.optimistic?.[id]?.get(row[resource.description.internal_id_col]);
          return pending ? { ...row, ...pending.values } : row;
        };
        // Spark, Ibis and custom providers keep their own apply_edit, outside Store ops
        if (resource.transactional !== false) resource.apply_edit = (edit) => this.applyTableEdit(id, edit);
      }
    }
    this.doc.__pret_resources__ = this.resources;
  }

  private setRevision(revision: number): void {
    this.revision = this.doc.revision = revision;
    if (this.database) this.database.revision = revision;
  }

  // Paged readers subscribe their cached identities without requesting offscreen row contents
  private readRemoteChanges(): Promise<any> {
    const watched = this.dataframeSync === "paged" ? Object.fromEntries(
      Object.entries(this.resources).filter(([, resource]) => resource.transactional !== false)
        .map(([id, resource]) => [id, [...(resource.loadedRows?.().keys() ?? [])]])
    ) : undefined;
    return this.getRemoteDataChanges(this.revision, watched);
  }

  // Locate the Arrow streams describing complete tables in a Store response
  private readTableBatches(payload: any): BrowserResource[] {
    return payload.tables.map((table) => {
      const parts = table.parts.map((part) => payload.bytes.subarray(part.offset, part.offset + part.length));
      return { resource_id: table.resource_id, bytes: parts[0], batches: parts.slice(1) };
    });
  }

  // Apply accepted rows and ordinary values together, replacing the pending local edits, then refresh queries
  private applyRemoteChanges(payload: any, op?: Op, result?: any): void {
    if (!op && payload.revision <= this.revision) return;
    const commits = payload.commits.filter((commit) => commit.revision > this.revision);
    if (this.database) {
      this.database.rollback();
      this.database.importTables(
        this.readTableBatches(payload).filter((input) => !this.database.hasResource(input.resource_id))
      );
      this.database.applyCommits(commits, payload.bytes);
    }
    // Keep selections on inserted rows by mapping their temporary IDs to the IDs Python assigned
    if (op && result?.status === "applied") {
      op.edits.forEach((edit, idx) => {
        const ids = result.row_identities?.[idx] || [];
        const aliases = this.identityAliases.get(edit.resource_id) || new Map();
        edit.temporary_ids?.forEach((id, pos) => {
          if (id < 0) aliases.set(id, ids[pos]);
        });
        this.identityAliases.set(edit.resource_id, aliases);
      });
    }
    // Local values of the op are replaced by the commits Python saved, unless its own commit is all that changed
    let kept = null;
    if (op?.inverse) {
      const [own] = commits;
      if (commits.length === 1 && own.request_id === op.request_id && sameJSON(own.ops, op.ops))
        kept = own;
      else this.doc.rollback(op.inverse);
      op.inverse = undefined;
    }
    for (const commit of commits) {
      if (commit === kept) continue;
      const { revision, request_id, origin, kind } = commit;
      this.doc.applyRemote(commit.ops, { revision, request_id, origin, kind });
      // An awaited Python callback of this page joins its undo history, as StoreDoc.edit_transaction records it
      if (origin === this.origin && kind === "edit" && request_id !== this.activeOp?.request_id) {
        this.canUndoOp = true;
        this.canRedoOp = false;
      }
    }
    this.setRevision(payload.revision);
    for (const { resource_id } of payload.tables) {
      if (!this.resources[resource_id])
        this.bindResources({ [resource_id]: { __pret_dataframe__: true, resource_id, binding_id: resource_id } });
    }
    for (const [id, resource] of Object.entries(this.resources)) {
      if (resource.transactional === false) continue;
      // Rows changed by the commits, starting from the values shown before a pending optimistic edit
      const values = new Map<unknown, Record<string, unknown>>(
        [...(op?.optimistic?.[id] ?? [])].map(([key, row]) => [key, row.before])
      );
      for (const commit of commits) {
        for (const change of commit.changes) {
          if (change.resource_id !== id || change.kind === "delete") continue;
          const rows = decodeDataFrameRows(payload.bytes.subarray(change.offset, change.offset + change.length));
          for (const row of rows) {
            const key = row[resource.description?.internal_id_col ?? INTERNAL_ROW_ID];
            values.set(key, { ...values.get(key), ...row });
          }
        }
      }
      // Local copies already showed the cell values of their own edit
      const summaries = commits.flatMap((commit) => commit.summaries.filter((change) =>
        !(this.database && op?.kind === "edit" && commit.request_id === op.request_id && change.kind === "update")));
      if (this.database) {
        const loaded = resource.loadedRows?.() ?? new Map();
        const columns = new Set<string>();
        for (const [key, patch] of values) {
          const pending = op?.optimistic?.[id]?.get(key);
          const row = pending ? { ...loaded.get(key), ...pending.values } : loaded.get(key);
          if (row) for (const column of Object.keys(patch))
            if (Object.prototype.hasOwnProperty.call(row, column) && !Object.is(row[column], patch[column]))
              columns.add(column);
        }
        if (columns.size) summaries.push({ resource_id: id, kind: "update", columns: [...columns] });
      }
      resource.updateRows?.(values);
      resource.acceptChanges?.(payload.revision, summaries);
    }
    if (op) op.optimistic = undefined;
    this.notifyHistory();
  }

  // Poll Python for missed commits only while something watches this Store
  startPolling(): void {
    if (!this.getRemoteDataChanges || this.pollTimer || this.phase === "loading" || !this.watched) return;
    this.pollTimer = setInterval(() => {
      if (!this.watched) {
        clearInterval(this.pollTimer);
        this.pollTimer = null;
      } else if (this.connected) void this.catchUpRemoteChanges().catch(() => {});
    }, 2000);
  }

  private get watched(): boolean {
    return this.listeners.size > 0 || this.doc.observed;
  }

  // Poll or respond to a Store notification, changes arriving during an op are fetched after it
  private catchUpRemoteChanges(): Promise<void> {
    if (!this.getRemoteDataChanges || this.phase === "loading") return Promise.resolve();
    if (this.phase !== "ready") {
      this.catchUpRequested = true;
      return Promise.resolve();
    }
    if (this.catchUpPromise) return this.catchUpPromise;
    this.catchUpRequested = false;
    this.catchUpPromise = this.readRemoteChanges()
      .then((payload) => {
        if (this.phase === "ready") this.applyRemoteChanges(payload);
      })
      .catch((error) => {
        this.connected = false;
        this.error = errorText(error);
        this.notifyHistory();
        throw error;
      })
      .finally(() => {
        this.catchUpPromise = null;
      });
    return this.catchUpPromise;
  }

  // Proxies and table edits call this before changing the Store, only ready or editing Stores accept edits
  assertWritable(): void {
    if (this.phase === "loading") throw new Error(this.error || "The Store is loading");
    if (!this.connected) {
      this.onRejectedWrite?.("disconnected");
      throw new Error("Editing is paused while the Store is disconnected");
    }
    if (this.phase === "pending" || this.phase === "uncertain")
      throw new Error("The previous Store op is awaiting acceptance");
  }

  private notifyHistory(): void {
    const enabled = this.connected && (this.phase === "ready" || this.phase === "editing");
    this.historySnapshot = {
      canUndo: enabled && this.canUndoOp,
      canRedo: enabled && this.canRedoOp,
      pending: this.phase === "loading" || this.phase === "pending" || this.phase === "uncertain",
      connected: this.connected,
      error: this.error,
      revision: this.revision,
    };
    for (const listener of this.listeners) listener();
  }

  // StoreDoc calls this when a local transaction begins, table edits made during the transaction join its op
  beginLocalOp(): void {
    this.assertWritable();
    this.phase = "editing";
    this.activeOp = {
      request_id: randomUUID(), origin: this.origin, kind: "edit", base_revision: this.revision, ops: [], edits: [],
    };
    this.database?.begin();
  }

  // StoreDoc calls this when the local transaction ends, submit its op
  finishLocalTransaction(transaction: LocalTransaction): void {
    const op = this.activeOp;
    if (this.phase !== "editing") return;
    op.ops = transaction.ops;
    op.inverse = transaction.inverse;
    if (transaction.error !== undefined) {
      const error = errorText(transaction.error);
      this.phase = "pending";
      this.opPromise = Promise.resolve().then(() => {
        this.rejectOp(op, { status: "conflict", error });
        return { status: "conflict", error };
      });
      this.notifyHistory();
      return;
    }
    if (!op.edits.length && !op.ops.length) {
      this.database?.rollback();
      this.activeOp = null;
      this.phase = "ready";
      return;
    }
    this.phase = "pending";
    this.error = null;
    this.notifyHistory();
    // Show the new cell values in loaded rows, and replace local queries depending on edited columns
    for (const [id, rows] of Object.entries(op.optimistic ?? {}))
      this.resources[id].updateRows?.(new Map([...rows].map(([key, row]) => [key, row.values])));
    if (this.database) {
      const summaries = Object.entries(op.optimistic ?? {}).map(([resource_id, rows]) => ({
        resource_id, kind: "update",
        columns: [...new Set([...rows.values()].flatMap((row) => Object.keys(row.values)
          .filter((column) => !Object.is(row.before[column], row.values[column]))))],
      }));
      for (const edit of op.edits) for (const item of edit.kind === "batch" ? edit.edits : [edit])
        if (item.kind === "insert" || item.kind === "delete")
          summaries.push({ resource_id: edit.resource_id, kind: item.kind, columns: [] });
      if (!this.commitRemoteOp) {
        this.database.release();
        this.setRevision(this.revision + 1);
      }
      for (const resource of Object.values(this.resources))
        if (resource.transactional !== false) resource.acceptChanges?.(this.revision, summaries);
    }
    if (this.commitRemoteOp) this.opPromise = this.submitOp(op);
    else {
      const result = { status: "applied", revision: this.revision };
      this.acceptOp(op, result);
      this.opPromise = Promise.resolve(result);
    }
    void this.opPromise.catch(() => {});
  }

  // Send the op, retrying with the same op ID if the previous reply was lost
  private async submitOp(op: Op): Promise<any> {
    try {
      const { optimistic, inverse, ...request } = op;
      const result = await this.commitRemoteOp(request);
      this.applyRemoteChanges(await this.readRemoteChanges(), op, result);
      if (result.status === "applied") this.acceptOp(op, result);
      else this.rejectOp(op, result);
      return result;
    } catch (error) {
      // Keep editing paused until Python tells us whether this op was saved
      this.error = errorText(error);
      this.connected = false;
      this.phase = "uncertain";
      this.notifyHistory();
      throw error;
    }
  }

  private finishOp(result: any, error: string | null): void {
    this.canUndoOp = Boolean(result.can_undo);
    this.canRedoOp = Boolean(result.can_redo);
    this.activeOp = null;
    this.phase = "ready";
    this.error = error;
    this.notifyHistory();
    if (this.catchUpRequested) void this.catchUpRemoteChanges().catch(() => {});
  }

  private acceptOp(op: Op, result: any): void {
    if (this.activeOp !== op) return;
    op.inverse = undefined;
    this.finishOp(result, null);
  }

  // Restore the values shown before the op, then apply what Python saved
  private rejectOp(op: Op, result: any): void {
    this.database?.rollback();
    for (const [id, rows] of Object.entries(op.optimistic ?? {}))
      this.resources[id].updateRows?.(new Map([...rows].map(([key, row]) => [key, row.before])));
    op.optimistic = undefined;
    if (op.inverse) this.doc.rollback(op.inverse);
    op.inverse = undefined;
    this.connected = !result.blocked;
    this.finishOp(
      { can_undo: result.can_undo ?? this.canUndoOp, can_redo: result.can_redo ?? this.canRedoOp },
      result.error || "The Store op was rejected"
    );
  }

  applyTableEdit(resourceId: string, op: any): Promise<any> {
    if (this.phase === "loading") return this.ready.then(() => this.applyTableEdit(resourceId, op));
    this.assertWritable();
    op = { ...op, base_revision: op.base_revision ?? this.revision };
    let result: any;
    const apply = () => {
      try {
        if (this.database) {
          result = this.database.applyEdit(resourceId, op, this.commitRemoteOp !== null);
        }
        const resource = this.resources[resourceId];
        const pending = this.activeOp.optimistic ??= {};
        const rows = pending[resourceId] ??= new Map();
        const loaded = resource.loadedRows?.() ?? new Map();
        for (const edit of op.kind === "batch" ? op.edits : [op]) {
          if (edit.kind && edit.kind !== "update") continue;
          const key = edit.row_identity;
          const row = loaded.get(key);
          if (!row) continue;
          const values = resource.normalizeValues(edit.values ?? { [edit.column]: edit.value });
          const previous = rows.get(key);
          rows.set(key, {
            before: { ...Object.fromEntries(Object.keys(values).map((column) => [column, row[column]])),
              ...previous?.before },
            values: { ...previous?.values, ...values },
          });
        }
        this.activeOp.edits.push({
          resource_id: resourceId,
          ...op,
          temporary_ids: this.commitRemoteOp ? result?.row_identities : undefined,
        });
      } catch (error) {
        this.doc.abort(error);
        throw error;
      }
    };
    if (this.phase === "editing") {
      apply();
      return Promise.resolve(result || { status: "applied", revision: this.revision });
    }
    this.doc.transact(apply);
    return this.opPromise.then((accepted) => ({ ...result, ...accepted }));
  }

  // Ask Python to reverse the latest op of this page, browser only Stores have no undo
  private async reverse(kind: "undo" | "redo"): Promise<boolean> {
    await this.ready;
    this.assertWritable();
    if (!this.commitRemoteOp || !(kind === "undo" ? this.canUndoOp : this.canRedoOp)) return false;
    const op: Op = { request_id: randomUUID(), origin: this.origin, kind, base_revision: this.revision, ops: [], edits: [] };
    this.activeOp = op;
    this.phase = "pending";
    this.notifyHistory();
    this.opPromise = this.submitOp(op);
    return (await this.opPromise)?.status === "applied";
  }

  undo = (): Promise<boolean> => this.reverse("undo");
  redo = (): Promise<boolean> => this.reverse("redo");
  get canUndo(): boolean {
    return this.historySnapshot.canUndo;
  }
  get canRedo(): boolean {
    return this.historySnapshot.canRedo;
  }
  getSnapshot = (): HistorySnapshot => this.historySnapshot;
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    this.startPolling();
    return () => this.listeners.delete(listener);
  };

  // pret.manager handle_state_change_msg calls this when Python saves a Store change
  notifyRemoteChange(): void {
    void this.catchUpRemoteChanges().catch(() => {});
  }

  setConnected(connected: boolean): void {
    // Comm message reasons also notify connection listeners, only reconnecting needs recovery
    if (this.connected === connected) return;
    this.connected = connected;
    this.notifyHistory();
    if (!connected) return;
    if (this.phase === "uncertain") {
      this.phase = "pending";
      this.opPromise = this.submitOp(this.activeOp);
      void this.opPromise.catch(() => {});
    } else if (this.getRemoteDataChanges) void this.catchUpRemoteChanges().catch(() => {});
  }
}

// pret.store rebuild_doc calls this while decoding a Store in the browser
// Reuse its controller when another widget receives the same Store
export function attachStore(
  doc: StoreDoc,
  resources: Record<string, any>,
  commitRemoteOp: CommitRemoteOp | null,
  origin: string,
  getRemoteDataChanges: GetRemoteDataChanges | null = null,
  dataframeSync: "full" | "paged" = "full"
): StoreController {
  const existing = doc.__pret_controller__ as StoreController | null;
  if (existing) {
    if (existing.dataframeSync !== dataframeSync)
      throw new Error("A Store cannot mix full and paged copies in one page");
    existing.bindResources(resources);
    return existing;
  }
  const id = doc.__pret_binding_id__;
  const bindings = Object.fromEntries(
    (pendingBindings.get(id) || []).map((resource) => [resource.resource_id, resource])
  );
  const controller = new StoreController(
    doc,
    { ...resources, ...bindings },
    commitRemoteOp,
    origin,
    getRemoteDataChanges,
    dataframeSync
  );
  if (id) {
    controllers.set(id, controller);
    pendingBindings.delete(id);
  }
  return controller;
}

// pret.dataframe rebuild_dataframe calls this while decoding a DataFrame kept in a Store
// Wait for attachStore if the DataFrame arrives before its Store
export function bindDataFrameResource(resource: any, ownerId: string, transactional: boolean): any {
  resource.transactional = transactional;
  const controller = controllers.get(ownerId);
  if (controller) controller.bindResources({ [resource.resource_id]: resource });
  else pendingBindings.set(ownerId, [...(pendingBindings.get(ownerId) ?? []), resource]);
  return resource;
}

export function getStoreHistory(store: any): StoreController | null {
  if (store?.controller) return store.controller;
  return (store?.[NODE]?.doc.__pret_controller__ as StoreController) || null;
}

// pret.hooks use_store_history calls this hook through the browser loader
// Update React undo and redo controls when history or connection status changes
export function useStoreHistory(store: any) {
  const history = getStoreHistory(store);
  if (!history) throw new Error("This Store does not have an attached history controller");
  const historySnapshot = useSyncExternalStore(history.subscribe, history.getSnapshot, history.getSnapshot);
  return { ...historySnapshot, undo: history.undo, redo: history.redo };
}
