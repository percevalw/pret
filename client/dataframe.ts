import { useEffect, useRef, useState } from "react";
import { tableFromIPC, type Field } from "apache-arrow";
import { decodeDataFrameRows, parseCellValue, displayValue } from "./dataframeValueConversions";
import { errorText, randomUUID } from "./store";
import { createBrowserDatabase, type BrowserDatabase, type BrowserResource } from "./dataframeSqlite";

export type QueryOptions = {
  filter?: unknown;
  sort?: { column: string; direction: "asc" | "desc" }[];
  columns?: string[];
  offset?: number;
  limit?: number;
  identities?: (string | number)[];
  locate?: { column: string; values: unknown[] };
  distinct?: string;
  // Page readers already know the filtered count, source providers may skip recounting
  include_count?: boolean;
  paging?: boolean;
};

export type DataFrameDescription = {
  columns: Array<{ name: string; type: string; nullable: boolean; normalized?: boolean; source_filter?: boolean }>;
  row_count: number | null;
  // Source previews keep loaded rows locally, null enables replacement by page and a number caps the result
  row_limit?: number | null;
  revision?: number;
  // Name of the provider row ID column, separate from application columns such as id or entity UUID
  internal_id_col?: string;
  capabilities: { edit: boolean; source_filters: boolean; changes: boolean; stable_identity?: boolean };
};

// Position lookup returns positions without an Arrow page
export type QueryResult = {
  bytes?: Uint8Array;
  row_count?: number;
  total_count: number | null;
  revision: number;
  offset?: number;
  positions?: Array<number | null>;
};

// Store commits group these row edits and return the identities assigned to insertions
export type DataFrameEdit = {
  request_id?: string;
  origin?: string;
  base_revision?: number;
  kind?: "insert" | "update" | "delete" | "batch";
  row_identity?: string | number;
  identity?: Record<string, unknown>;
  column?: string;
  value?: unknown;
  values?: Record<string, unknown>;
  edits?: DataFrameEdit[];
};
export type DataFrameEditResult = {
  status: "applied" | "conflict" | "error";
  revision?: number;
  request_id?: string;
  row_identity?: string | number;
  row_identities?: number[];
  error?: string;
  message?: string;
};

// Local and remote sources expose the same queries, while the Store coordinates edits
export interface DataFrameProvider {
  describe(): Promise<DataFrameDescription>;
  query(options: QueryOptions): Promise<QueryResult>;
  apply_edit(edit: DataFrameEdit): Promise<DataFrameEditResult>;
  get_changes_after_revision(revision: number): Promise<{ revision: number; refresh?: boolean }>;
}

// pret.dataframe rebuild_dataframe calls this for embedded Arrow data
// Hold the bytes until attachStore creates the shared browser database
export function createOfflineDataFrame(resource_id: string, binding_id: string, bytes: Uint8Array): ClientDataFrame {
  return createClientDataFrame({
    __pret_dataframe__: true,
    resource_id,
    binding_id,
    offlineRows: { resource_id, bytes },
  });
}

// Rows of a DataFrame shared by all its widgets in a page, a filter with its row count, plus the loaded rows
// of a Spark or Ibis preview. Each widget view adds its own filter and sort on top of it
export type QueryScope = {
  filter: unknown;
  row_count: number;
  revision: number;
  rows?: DataFrameRow[] | null;
  bytes?: Uint8Array;
  offset?: number;
};
export type DataFrameRow = Record<string, any>;
// Widgets can request pages or identities without depending on a particular grid component
export type DataFrameRowSource = {
  rowCount: number;
  revision: string | number;
  subscribeRows(listener: (indices: readonly number[]) => void): () => void;
  getRows(start: number, end: number, signal: AbortSignal): Promise<DataFrameRow[]>;
  getCachedRow(index: number): DataFrameRow | undefined;
  findRowIndex(key: string | number, signal: AbortSignal): Promise<number>;
  getRowsAtIndices(indices: number[], signal: AbortSignal): Promise<DataFrameRow[]>;
  getRowsByKeys(keys: unknown[], signal: AbortSignal): Promise<DataFrameRow[]>;
};
export type DataFrameView = DataFrameRowSource & {
  disposed: boolean;
  cache: ReadonlyMap<number, DataFrameRow>;
  identityRows: Map<unknown, DataFrameRow>;
  dependencies: Map<string, Set<string>>;
  publishRows(indices: number[]): void;
};
// Share column descriptions and change checks across widgets using the same DataFrame
// Each widget view keeps its own filters, sort order and recently fetched pages
//
// useDataFrameView shows the whole table by default. Loaders of related tables, such as Metanno
// relations, instead fetch the scopes of several tables with fetchScope and show them together
// with setScope, use setStatus while querying, and set loadPage to fetch another page of a
// loaded source when a widget scrolls past it
export class ClientDataFrame implements DataFrameProvider {
  declare __pret_dataframe__: true;
  declare resource_id: string;
  declare binding_id: string;
  // Store binding installs local methods or keeps the methods supplied by the Python provider
  declare describe: DataFrameProvider["describe"];
  declare query: DataFrameProvider["query"];
  declare apply_edit: DataFrameProvider["apply_edit"];
  declare get_changes_after_revision: DataFrameProvider["get_changes_after_revision"];
  declare pendingValues?: (row: DataFrameRow) => DataFrameRow;
  declare resolve_identity?: (value: unknown) => unknown;
  // Sources that push change notifications do not need the DataFrame polling timer
  declare subscribeChanges?: (listener: () => void) => () => void;
  // Set while decoding the Store: its controller, whether edits join Store ops (false for Spark, Ibis and custom
  // providers), a complete browser copy in a connected Store, or the Arrow rows of a browser only Store
  declare controller?: any;
  declare transactional?: boolean;
  declare localCopy?: boolean;
  declare offlineRows?: BrowserResource;

  description: DataFrameDescription | null = null;
  scope: QueryScope | null = null;
  // Query filter entered by the user for the current scope, set by setScope callers
  sourceFilter: unknown = null;
  revision = 0;
  viewVersion = 0;
  contentVersion = 0;
  readonly fields = new Map<string, Field>();
  loading: string | null = null;
  error: string | null = null;
  private descriptionPromise: Promise<void> | null = null;
  private scopePromise: Promise<void> | null = null;
  private listeners = new Set<() => void>();
  private views = new Set<DataFrameView>();
  // The recipe can load related tables along with a requested source page
  loadPage: ((offset: number) => Promise<unknown>) | null = null;
  private pagePromise: Promise<unknown> | null = null;
  private loadedDatabase: Promise<BrowserDatabase> | null = null;
  private checkingChanges = false;
  private changesRequested = false;
  private changeTimer: ReturnType<typeof setInterval> | null = null;
  private unsubscribeChanges: (() => void) | null = null;

  // The browser loader supplies provider methods and Store binding metadata
  constructor(source: { resource_id: string; binding_id: string; [key: string]: any }) {
    Object.assign(this, source);
  }

  subscribe(listener: () => void) {
    this.listeners.add(listener);
    this.startPolling();
    const subscribeChanges = this.subscribeChanges ?? this.controller?.subscribe;
    if (!this.unsubscribeChanges && subscribeChanges)
      this.unsubscribeChanges = subscribeChanges.call(this, () => {
        void this.refreshChanges();
        this.notifySubscribers();
      });
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0) {
        if (this.changeTimer !== null) clearInterval(this.changeTimer);
        this.changeTimer = null;
        this.unsubscribeChanges?.();
        this.unsubscribeChanges = null;
        void this.loadedDatabase?.then((database) => database.close(), () => {});
        this.loadedDatabase = null;
        this.invalidateViews(false);
      }
    };
  }

  // Poll paged sources while mounted, local tables notify subscribers through their Store
  private startPolling() {
    if (
      this.subscribeChanges ||
      this.changeTimer !== null ||
      this.listeners.size === 0 ||
      !this.description?.capabilities.changes
    )
      return;
    this.changeTimer = setInterval(() => void this.refreshChanges(), 2000);
  }

  notifySubscribers() {
    this.listeners.forEach((listener) => listener());
  }

  setStatus(loading: string | null, error: string | null = null) {
    this.loading = loading;
    this.error = error;
    this.notifySubscribers();
  }

  async ensureDescription() {
    if (this.description) return;
    if (!this.descriptionPromise) {
      this.setStatus("Loading data");
      this.descriptionPromise = this.describe()
        .then((description) => {
          this.description = description;
          this.revision = description.revision ?? 0;
          this.startPolling();
        })
        .catch((reason) => {
          this.error = errorText(reason);
          throw reason;
        })
        .finally(() => {
          this.loading = null;
          this.notifySubscribers();
        });
    }
    await this.descriptionPromise;
  }

  // Show the whole table once when no scope is set, for useDataFrameView, a scope set meanwhile by a loader is kept
  async ensureScope() {
    await this.ensureDescription();
    if (this.scope) return;
    if (!this.scopePromise) {
      this.setStatus("Loading data");
      this.scopePromise = this.fetchScope(null)
        .then((scope) => {
          if (!this.scope) this.setScope(scope);
        })
        .catch((reason) => {
          this.setStatus(null, errorText(reason));
          throw reason;
        });
    }
    await this.scopePromise;
  }

  // Query the scope of a filter without showing it, the count of a Store table or the loaded rows of a source
  // preview starting at offset, setScope then shows it
  async fetchScope(filter: unknown, sort: QueryOptions["sort"] = [], offset = 0): Promise<QueryScope> {
    await this.ensureDescription();
    const limit = this.description.row_limit;
    const loaded = limit !== undefined;
    const result = await this.query({ filter, sort, offset, limit: loaded ? limit ?? 500 : 0 });
    const rows = loaded ? this.decodeRows(result.bytes) : null;
    return { filter, row_count: limit != null ? rows.length : result.total_count!,
      revision: result.revision, rows, bytes: loaded ? result.bytes : undefined, offset };
  }

  // Show a scope in every widget of this DataFrame, their views are rebuilt and the loading status is cleared
  // notify false lets a loader set the scopes of related tables before widgets render, sourceFilter is the
  // query typed by the user
  setScope(scope: QueryScope, notify = true, sourceFilter: unknown = this.sourceFilter) {
    this.sourceFilter = sourceFilter;
    this.loading = null;
    this.error = null;
    if (this.scope !== scope) {
      void this.loadedDatabase?.then((database) => database.close(), () => {});
      this.loadedDatabase = null;
    }
    this.scope = scope;
    this.revision = Math.max(this.revision, scope.revision);
    this.invalidateViews(notify);
  }

  // Release every query view so mounted widgets create new ones for the current scope and revision
  invalidateViews(notify = true) {
    this.viewVersion++;
    for (const view of this.views) this.releaseView(view);
    if (notify) this.notifySubscribers();
  }

  // Source preview widgets filter loaded rows, stored DataFrames query their provider
  async createView(
    ast: unknown,
    serializedFilter: string,
    primaryKey?: string | null,
    sort: QueryOptions["sort"] = []
  ) {
    if (!this.scope || !this.description) return null;
    const filter =
      this.scope.filter && ast ? { kind: "and", children: [this.scope.filter, ast] } : this.scope.filter ?? ast;
    const generation = this.viewVersion;
    let scope = this.scope.rows || (!ast && this.scope.revision === this.revision)
      ? this.scope
      : await this.fetchScope(filter, sort);
    if (scope.rows && (ast || sort.length)) {
      // Widget filters query the shared loaded table
      if (!this.loadedDatabase)
        this.loadedDatabase = createBrowserDatabase([{ resource_id: this.resource_id, bytes: scope.bytes }]);
      const database = await this.loadedDatabase;
      if (generation !== this.viewVersion) return null;
      const result = database.query(this.resource_id, { filter: ast, sort, limit: scope.rows.length });
      const rows = this.decodeRows(result.bytes);
      scope = { ...scope, rows, row_count: rows.length, offset: 0 };
    }
    if (generation !== this.viewVersion) return null;
    if (scope.revision < this.revision) return null;
    if (scope.revision > this.revision) {
      this.revision = scope.revision;
      this.invalidateViews();
      return null;
    }
    const identity = primaryKey ?? this.description.internal_id_col!;
    const cache = new Map<number, DataFrameRow>(scope.rows?.map((row, idx) => [idx + (scope.offset ?? 0), row]));
    const identityRows = new Map<unknown, DataFrameRow>();
    const rowListeners = new Set<(indices: readonly number[]) => void>();
    const dependencies = new Map([[this.resource_id, new Set([identity, this.description.internal_id_col, ...sort.map((item) => item.column)])]]);
    // Relation predicates also depend on the referenced table and its parent filter
    const visit = (node: any, resource = this.resource_id) => {
      if (!node) return;
      if (!dependencies.has(resource)) dependencies.set(resource, new Set());
      if (node.column) dependencies.get(resource).add(node.column);
      if (node.children) node.children.forEach((child) => visit(child, resource));
      if (node.child) visit(node.child, resource);
      if (node.kind === "relation") {
        if (!dependencies.has(node.resource_id)) dependencies.set(node.resource_id, new Set());
        dependencies.get(node.resource_id).add(node.parent_column);
        visit(node.filter, node.resource_id);
      }
    };
    visit(filter);
    let view: DataFrameView;
    const current = (signal: AbortSignal) => !signal.aborted && !view.disposed;
    const read = async (options: QueryOptions, signal: AbortSignal) => {
      if (!current(signal)) return null;
      let result = await this.query({ filter, sort, include_count: false, ...options });
      if (result.revision < this.revision)
        result = await this.query({ filter, sort, include_count: false, ...options });
      if (!current(signal)) return null;
      if (result.revision !== this.revision) {
        this.revision = Math.max(this.revision, result.revision);
        this.invalidateViews();
        return null;
      }
      return result;
    };
    let pageSize = 256;
    let pendingRows: Promise<void> | null = null;
    const readRange = async (start: number, end: number, signal: AbortSignal) => {
      if (!current(signal)) return [];
      if (scope.rows) {
        const offset = scope.offset ?? 0;
        end = Math.min(end, scope.row_count);
        if ((start < offset || end > offset + scope.rows.length) && start < scope.row_count) {
          // Center the viewport in the next loaded window and replace its related tables
          if (!this.pagePromise) {
            const next = Math.max(0, start - Math.floor((500 - (end - start)) / 2));
            this.pagePromise = (this.loadPage ? this.loadPage(next) :
              this.fetchScope(this.scope.filter, sort, next).then((page) => this.setScope(page)))
              .finally(() => { this.pagePromise = null; });
          }
          await this.pagePromise;
          return [];
        }
        return scope.rows.slice(Math.max(0, start - offset), end - offset);
      }
      const rows: DataFrameRow[] = [];
      end = Math.min(end, scope.row_count);
      for (let offset = start; offset < end; ) {
        if (!current(signal)) return [];
        if (!cache.has(offset)) {
          // Scroll requests share the current fetch, obsolete ranges stop here before issuing another query
          if (pendingRows) {
            await pendingRows;
            continue;
          }
          // Share aligned pages across viewport changes, shrinking pages only when the source rejects their byte size
          pendingRows = (async () => {
            while (!view.disposed) {
              const pageStart = Math.floor(offset / pageSize) * pageSize;
              try {
                const result = await read(
                  { offset: pageStart, limit: Math.min(pageSize, scope.row_count - pageStart) },
                  new AbortController().signal
                );
                if (!result) return;
                this.decodeRows(result.bytes).forEach((row, index) => cache.set(pageStart + index, row));
                while (cache.size > 2048) cache.delete(cache.keys().next().value);
                return;
              } catch (reason) {
                if (pageSize === 1 ||
                    !errorText(reason).includes("Source query page exceeds the byte limit"))
                  throw reason;
                pageSize /= 2;
              }
            }
          })().finally(() => { pendingRows = null; });
          await pendingRows;
          if (!current(signal) || !cache.has(offset)) return [];
        }
        rows.push(cache.get(offset++)!);
      }
      return rows;
    };
    view = {
      disposed: false,
      cache,
      identityRows,
      dependencies,
      subscribeRows: (listener) => {
        rowListeners.add(listener);
        return () => { rowListeners.delete(listener); };
      },
      publishRows: (indices) => rowListeners.forEach((listener) => listener(indices)),
      rowCount: scope.row_count!,
      revision: `${scope.revision}:${generation}:${serializedFilter}:${JSON.stringify(sort)}`,
      getRows: readRange,
      getCachedRow: (index) => cache.get(index),
      findRowIndex: async (key, signal) => {
        if (scope.rows) {
          const idx = scope.rows.findIndex((row) => row[identity] === key);
          return current(signal) && idx >= 0 ? idx + (scope.offset ?? 0) : -1;
        }
        if (identity === this.description.internal_id_col && this.resolve_identity)
          key = this.resolve_identity(key) as typeof key;
        const result = await read({ locate: { column: identity, values: [key] }, limit: 0 }, signal);
        return result?.positions?.[0] ?? -1;
      },
      getRowsAtIndices: async (positions, signal) => {
        if (scope.rows) return current(signal) ? positions.map((idx) => cache.get(idx)).filter(Boolean) : [];
        // readRange shares aligned page requests across these positions
        const rows = await Promise.all(positions.map(async (idx) => cache.get(idx) ??
          (await readRange(idx, idx + 1, signal))[0]));
        return current(signal) ? rows.filter((row) => row !== undefined) : [];
      },
      getRowsByKeys: async (keys, signal) => {
        if (identity === this.description.internal_id_col && this.resolve_identity)
          keys = keys.map((key) => this.resolve_identity(key));
        const byKey = new Map<unknown, DataFrameRow>((scope.rows ?? [...identityRows.values(), ...cache.values()])
          .map((row) => [row[identity], row]));
        if (scope.rows) return current(signal) ? keys.map((key) => byKey.get(key)).filter(Boolean) : [];
        for (const key of identityRows.keys()) if (!keys.includes(key)) identityRows.delete(key);
        const missing = keys.filter((key) => !byKey.has(key));
        for (let offset = 0; offset < missing.length; offset += 256) {
          const predicate = {
            kind: "predicate",
            column: identity,
            operator: "in",
            value: missing.slice(offset, offset + 256),
          };
          const result = await read(
            {
              filter: filter ? { kind: "and", children: [filter, predicate] } : predicate,
              limit: Math.min(256, missing.length - offset),
            },
            signal
          );
          if (!result) return [];
          const rows: DataFrameRow[] = this.decodeRows(result.bytes);
          rows.forEach((row) => {
            byKey.set(row[identity], row);
            identityRows.set(row[identity], row);
          });
        }
        return keys.map((key) => byKey.get(key)).filter((row) => row !== undefined);
      },
    };
    this.views.add(view);
    return view;
  }

  // Keep Arrow types from query pages for schema exact optimistic input conversion
  decodeRows(bytes: Uint8Array): DataFrameRow[] {
    for (const field of tableFromIPC(bytes).schema.fields) this.fields.set(field.name, field);
    return decodeDataFrameRows(bytes).map((row) => this.pendingValues?.(row) ?? row);
  }

  // Convert input to the same displayed values used by Arrow query responses
  normalizeValues(values: DataFrameRow): DataFrameRow {
    return Object.fromEntries(Object.entries(values).map(([column, value]) => {
      const field = this.fields.get(column);
      if (!field) throw new Error(`Column ${column} has not been loaded`);
      return [column, displayValue(parseCellValue(value, field), field.type)];
    }));
  }

  // The Store watches only rows held by live query and identity readers
  loadedRows(): Map<unknown, DataFrameRow> {
    const identity = this.description?.internal_id_col;
    const rows = new Map<unknown, DataFrameRow>();
    for (const view of this.views) {
      for (const row of [...view.cache.values(), ...view.identityRows.values()]) rows.set(row[identity], row);
    }
    return rows;
  }

  // Changes to predicates and ordering replace only the views that depend on those columns
  acceptChanges(revision: number, summaries: Array<{ resource_id: string; kind: string; columns: string[] }>): void {
    this.revision = revision;
    if (this.scope && this.scope.revision >= 0) this.scope.revision = revision;
    let invalidated = false;
    for (const view of [...this.views]) {
      if (summaries.some((change) => view.dependencies.has(change.resource_id) &&
          (change.kind !== "update" || change.columns.some((column) =>
            view.dependencies.get(change.resource_id).has(column))))) {
        this.releaseView(view);
        invalidated = true;
      }
    }
    if (invalidated) {
      if (this.scope) this.scope.revision = -1;
      this.viewVersion++;
      this.notifySubscribers();
    }
  }

  // Publish effective cell changes while keeping unrelated row objects and query sources
  updateRows(values: Map<unknown, DataFrameRow>): void {
    const identity = this.description?.internal_id_col;
    let changed = false;
    const notifications: Array<[DataFrameView, number[]]> = [];
    for (const view of this.views) {
      const indices: number[] = [];
      const replace = (row: DataFrameRow) => {
        const patch = values.get(row[identity]);
        if (!patch || Object.keys(patch).every((key) => Object.is(row[key], patch[key]))) return row;
        changed = true;
        return { ...row, ...patch };
      };
      for (const [index, row] of view.cache) {
        const next = replace(row);
        if (next !== row) {
          (view.cache as Map<number, DataFrameRow>).set(index, next);
          indices.push(index);
        }
      }
      for (const [key, row] of view.identityRows) view.identityRows.set(key, replace(row));
      if (indices.length) notifications.push([view, indices]);
    }
    if (changed) {
      this.contentVersion++;
      for (const [view, indices] of notifications) view.publishRows(indices);
      this.notifySubscribers();
    }
  }

  releaseView(view: DataFrameView | null) {
    if (!view || view.disposed) return;
    view.disposed = true;
    this.views.delete(view);
  }

  async applyMutation(edit: DataFrameEdit) {
    if (!this.description?.capabilities.edit || !this.description.internal_id_col) return null;
    // Edits joining Store ops report their failure once through the Store history, the DataFrame error
    // shown by every view of this table stays for loading and query failures
    const report = (error: string | null) => {
      if (!this.controller || this.transactional === false) this.error = error;
      this.notifySubscribers();
      return error;
    };
    try {
      const result = await this.apply_edit({
        request_id: randomUUID(),
        base_revision: this.revision,
        ...edit,
      });
      if (result.status === "conflict") {
        report("Edit conflict, refresh before changing these values again");
      } else if (result.status === "applied") {
        this.error = null;
        await this.refreshChanges();
        this.notifySubscribers();
      } else {
        report(String(result.error ?? result.message ?? "Edit was rejected"));
      }
      return result;
    } catch (reason: any) {
      return { status: "error", error: report(errorText(reason)) };
    }
  }

  async applyEdit(row: DataFrameRow, column: string, value: unknown) {
    return this.applyMutation({
      row_identity: row[this.description!.internal_id_col!],
      column,
      value,
      kind: "update",
    });
  }

  async insert(values: DataFrameRow | DataFrameRow[]) {
    if (Array.isArray(values))
      return this.applyMutation({
        kind: "batch",
        edits: values.map((row): DataFrameEdit => ({ kind: "insert", values: row })),
      });
    return this.applyMutation({
      kind: "insert",
      values,
    });
  }

  async refreshChanges() {
    if (!this.description?.capabilities.changes) return;
    if (this.checkingChanges) {
      this.changesRequested = true;
      return;
    }
    this.changesRequested = false;
    this.checkingChanges = true;
    try {
      const changes = await this.get_changes_after_revision(this.revision);
      if (changes.revision <= this.revision) return;
      this.revision = changes.revision;
      this.invalidateViews();
    } catch (reason: any) {
      this.error = errorText(reason);
      this.notifySubscribers();
    } finally {
      this.checkingChanges = false;
      if (this.changesRequested) void this.refreshChanges();
    }
  }
}

// Repeated Python references to one binding share descriptions, scopes and query views
// Weak references let the DataFrame disappear when its Store and widgets release it
const clients = new Map<string, WeakRef<ClientDataFrame>>();
const collectedClients = new FinalizationRegistry<string>((key) => {
  if (!clients.get(key)?.deref()) clients.delete(key);
});

// pret.dataframe rebuild_dataframe calls this before attaching the provider to its Store
export function createClientDataFrame(source: { resource_id: string; binding_id: string; [key: string]: any }) {
  const key = `${source.resource_id}:${source.binding_id}`;
  let dataframe = clients.get(key)?.deref();
  if (!dataframe) {
    dataframe = new ClientDataFrame(source);
    clients.set(key, new WeakRef(dataframe));
    collectedClients.register(dataframe, key);
  }
  return dataframe;
}

// Components use this hook to subscribe to a DataFrame and release their query views, load false leaves
// the scope to a loader of related tables, such as Metanno factory widgets
export function useDataFrameView(
  dataframe: ClientDataFrame | null,
  filter: unknown = null,
  primaryKey: string | null = null,
  sort: QueryOptions["sort"] = [],
  load = true
) {
  const [, render] = useState(0);
  const [loaded, setLoaded] = useState<{ dataframe: ClientDataFrame; key: string; source: DataFrameView } | null>(null);
  const current = useRef<DataFrameView | null>(null);
  const filterKey = JSON.stringify(filter);
  const sortKey = JSON.stringify(sort);
  const key = JSON.stringify([primaryKey, filterKey, sortKey]);
  useEffect(() => {
    if (!dataframe) return;
    setLoaded(null);
    const unsubscribe = dataframe.subscribe(() => render((value) => value + 1));
    // ensureScope reports its errors through the DataFrame status
    if (load) dataframe.ensureScope().catch(() => {});
    return () => {
      unsubscribe();
      dataframe.releaseView(current.current);
      current.current = null;
    };
  }, [dataframe, load]);
  useEffect(() => {
    if (!dataframe?.scope || !dataframe.description) return;
    if (loaded?.dataframe === dataframe && loaded.key === key && !current.current?.disposed) return;
    let disposed = false;
    void dataframe
      .createView(filter, filterKey, primaryKey, sort)
      .then(async (view) => {
        if (!view) return;
        // Populate cached row positions before publishing a replacement query to mounted widgets
        if (current.current) {
          await view.getRowsAtIndices(
            [...current.current.cache.keys()].filter((index) => index < view.rowCount),
            new AbortController().signal
          );
        }
        if (disposed || view.disposed) {
          dataframe.releaseView(view);
          return;
        }
        dataframe.releaseView(current.current);
        current.current = view;
        setLoaded({ dataframe, key, source: view });
      })
      .catch((reason) => {
        if (!disposed) dataframe.setStatus(null, errorText(reason));
      });
    return () => {
      disposed = true;
    };
  }, [dataframe, dataframe?.viewVersion, dataframe?.description, filterKey, primaryKey, sortKey]);
  return {
    rowSource: loaded?.dataframe === dataframe && loaded?.key === key && !loaded.source.disposed ? loaded.source : null,
    previousRowSource: loaded?.dataframe === dataframe ? loaded.source : null,
    contentVersion: dataframe?.contentVersion ?? 0,
    description: dataframe?.description ?? null,
    loading: dataframe?.loading ?? (dataframe && !loaded && !dataframe.error ? "Loading data" : null),
    error: dataframe?.error ?? null,
  };
}
