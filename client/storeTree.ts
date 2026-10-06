// Ordinary Store values in the browser, a tree of nodes changed by the JSON operations of pret/store_ops.py.
//
// Each map or list is a StoreNode with a stable proxy, so a proxy captured as `todos[2]` keeps following its
// item when items are inserted before it. Each node caches a plain snapshot, an edit clears the snapshots of
// the edited node and its ancestors only, so React widgets showing untouched values do not render again.
//
// Edits of one local transaction are recorded with their inverses, the Store controller in store.ts sends them
// to Python and replays the inverses backwards if Python refuses them.

type JSONPrimitive = string | number | boolean | null;
export type JSONValue = JSONPrimitive | JSONObject | JSONArray;

export interface JSONObject {
  [k: string]: JSONValue;
}

export type JSONArray = JSONValue[];
export type Path = (string | number)[];
// Same operations as pret/store_ops.py, eg {"op": "insert", "path": ["data", "todos", 2], "value": "milk"}
export type StoreOp = { op: "set" | "delete" | "insert"; path: Path; value?: any };
// Operations of one local transaction and their inverses in application order, error makes it fail when it ends
export type LocalTransaction = { ops: StoreOp[]; inverse: StoreOp[]; error?: unknown };

export const DATAFRAME_REF_KEY = "__pret_dataframe_ref__";
export const NODE = Symbol("node");
export const JS = Symbol("js");

const isMarker = (value: any): boolean =>
  !!value && typeof value === "object" && !Array.isArray(value) && typeof value[DATAFRAME_REF_KEY] === "string";

export class StoreNode {
  proxy: any = null;
  // Plain copy of this value shared with React, cleared when this value or one of its descendants changes
  snap: JSONObject | JSONArray | undefined = undefined;

  constructor(
    readonly doc: StoreDoc,
    readonly children: Record<string, any> | any[],
    public parent: StoreNode | null
  ) {}

  get isList(): boolean {
    return Array.isArray(this.children);
  }

  snapshot(): JSONObject | JSONArray {
    if (this.snap === undefined) {
      const read = (child: any) => (child instanceof StoreNode ? child.snapshot() : child);
      this.snap = Array.isArray(this.children)
        ? this.children.map(read)
        : Object.fromEntries(Object.entries(this.children).map(([key, child]) => [key, read(child)]));
    }
    return this.snap;
  }

  // Operation path of this node, or null once it was removed from its Store
  path(): Path | null {
    const path: Path = [];
    let node: StoreNode = this;
    while (node.parent) {
      const siblings = node.parent.children;
      const key = Array.isArray(siblings)
        ? siblings.indexOf(node)
        : Object.keys(siblings).find((name) => siblings[name] === node);
      if (key === undefined || key === -1) return null;
      path.push(key);
      node = node.parent;
    }
    return node === this.doc.root ? path.reverse() : null;
  }
}

// Turn a JSON value into nodes under parent, nodes removed by an edit are moved back unchanged on rollback.
// Python sends integers above 2^32 as 64 bit CBOR integers, decoded as BigInt, Store integers fit in numbers
function adopt(doc: StoreDoc, value: any, parent: StoreNode | null): any {
  if (typeof value === "bigint") return Number(value);
  if (value instanceof StoreNode) {
    value.parent = parent;
    return value;
  }
  if (Array.isArray(value)) {
    const node = new StoreNode(doc, [], parent);
    for (const child of value) (node.children as any[]).push(adopt(doc, child, node));
    return node;
  }
  if (value && typeof value === "object" && !isMarker(value)) {
    const node = new StoreNode(doc, {}, parent);
    for (const [key, child] of Object.entries(value)) node.children[key] = adopt(doc, child, node);
    return node;
  }
  return value;
}

// Copy a value assigned through a proxy, proxies become their values and DataFrames references to their table
export function cloneJSONValue(value: any): JSONValue {
  value = value?.[JS] ?? value;
  // DataFrame rows hold 64 bit integers as BigInt, Store numbers are exact JavaScript numbers as in Python
  if (typeof value === "bigint") {
    if (!Number.isSafeInteger(Number(value))) throw new Error(`Store integers must be within ±2^53 - 1, got ${value}`);
    return Number(value);
  }
  if (value?.__pret_dataframe__) return { [DATAFRAME_REF_KEY]: value.resource_id };
  if (Array.isArray(value)) return value.map(cloneJSONValue);
  if (value && typeof value === "object") {
    const out: JSONObject = {};
    for (const [key, child] of Object.entries(value)) out[key] = cloneJSONValue(child);
    return out;
  }
  return value === undefined ? null : value;
}

// Compare JSON values sent by Python and the page, whose integers may be BigInt or numbers
export const sameJSON = (a: unknown, b: unknown): boolean => {
  const text = (value: unknown) => JSON.stringify(value, (_, item) => (typeof item === "bigint" ? Number(item) : item));
  return text(a) === text(b);
};

// Clear the cached snapshots from an edited node up to the root
function invalidate(node: StoreNode | null): void {
  while (node && node.snap !== undefined) {
    node.snap = undefined;
    node = node.parent;
  }
}

// Apply one operation in place and return its inverse, as store_ops.apply does in Python
function applyOp(doc: StoreDoc, op: StoreOp): StoreOp {
  const { path } = op;
  let container: StoreNode = doc.root;
  for (const part of path.slice(0, -1)) {
    const child = container.children[part as any];
    if (!(child instanceof StoreNode) || !(part in container.children))
      throw new Error(`Cannot apply ${op.op} at ${JSON.stringify(path)}`);
    container = child;
  }
  const key = path[path.length - 1];
  const children: any = container.children;
  const isList = Array.isArray(children);
  const inRange = isList && typeof key === "number" && key >= 0 && key < children.length;
  let inverse: StoreOp | null = null;
  if (op.op === "set" && !isList && typeof key === "string") {
    inverse = key in children ? { op: "set", path, value: children[key] } : { op: "delete", path };
    children[key] = adopt(doc, op.value, container);
  } else if (op.op === "set" && inRange) {
    inverse = { op: "set", path, value: children[key] };
    children[key] = adopt(doc, op.value, container);
  } else if (op.op === "delete" && !isList && typeof key === "string" && key in children) {
    inverse = { op: "set", path, value: children[key] };
    delete children[key];
  } else if (op.op === "delete" && inRange) {
    inverse = { op: "insert", path, value: children.splice(key, 1)[0] };
  } else if (op.op === "insert" && isList && typeof key === "number" && key >= 0 && key <= children.length) {
    children.splice(key, 0, adopt(doc, op.value, container));
    inverse = { op: "delete", path };
  }
  if (!inverse) throw new Error(`Cannot apply ${op.op} at ${JSON.stringify(path)}`);
  invalidate(container);
  return inverse;
}

// Ops given to subscribers and history listeners, eg {revision, origin, kind} of a Python commit
export type ChangeInfo = Record<string, unknown>;

// Store controller methods called by the document, see StoreController in store.ts
export type DocController = {
  assertWritable(): void;
  beginLocalOp(): void;
  finishLocalTransaction(transaction: LocalTransaction): void;
};

// Values of one Store in a page, rebuilt by pret.store rebuild_doc from the state Python sent
export class StoreDoc {
  root: StoreNode;
  // Open local transaction, its operations are sent to Python as one op when it ends
  transaction: LocalTransaction | null = null;
  // Set by attachStore, sends local ops to Python and applies the commits it saved
  __pret_controller__: DocController | null = null;
  // DataFrames referenced by the Store values, by resource ID, and the ID shared by the widgets of this Store
  __pret_resources__: Record<string, any> = {};
  __pret_binding_id__: string | null = null;
  private listeners = new Set<() => void>();
  private subscribers = new Set<{ node: StoreNode; callback: (ops: StoreOp[], info: ChangeInfo) => void }>();

  constructor(state: JSONObject, public revision = 0) {
    this.root = adopt(this, state, null);
  }

  // Return the value at a path, a proxy for maps and lists, the DataFrame of a table reference
  proxyAt(path: Path): any {
    let child: any = this.root;
    for (const part of path) child = child.children[part];
    return this.wrap(child);
  }

  wrap(child: any): any {
    if (child instanceof StoreNode) return child.proxy ?? (child.proxy = makeProxy(child));
    if (isMarker(child)) return this.__pret_resources__[child[DATAFRAME_REF_KEY]] ?? child;
    return child;
  }

  // Open a local transaction, or return null when one is already open and this call joins it
  begin(): LocalTransaction | null {
    if (this.transaction) return null;
    this.__pret_controller__?.beginLocalOp();
    this.transaction = { ops: [], inverse: [] };
    return this.transaction;
  }

  // Make the open transaction fail when it ends, its controller then also discards its table edits
  abort(error: unknown): void {
    if (!this.transaction) throw new Error("No open Store transaction can be aborted");
    this.transaction.error ??= error;
  }

  // Close the local transaction, the controller sends it, a failed transaction without controller is reverted
  end(transaction: LocalTransaction, error?: unknown): void {
    if (this.transaction !== transaction) throw new Error("endTransaction called with a non-active transaction.");
    if (error !== undefined) this.abort(error);
    this.transaction = null;
    if (transaction.ops.length) this.emit(transaction.ops, { local: true });
    if (this.__pret_controller__) this.__pret_controller__.finishLocalTransaction(transaction);
    else if (transaction.error !== undefined) this.rollback(transaction.inverse);
  }

  // Run fn in a local transaction, a failure reverts the values and is raised again
  transact<T>(fn: () => T): T {
    const transaction = this.begin();
    if (!transaction) return fn();
    let result: T;
    try {
      result = fn();
    } catch (error) {
      this.end(transaction, error);
      throw error;
    }
    this.end(transaction);
    return result;
  }

  // Apply a proxy edit in the open local transaction, or as its own transaction
  edit(op: StoreOp): void {
    this.__pret_controller__?.assertWritable();
    this.transact(() => {
      this.transaction.inverse.push(applyOp(this, op));
      this.transaction.ops.push(op);
    });
  }

  // Apply operations saved by Python
  applyRemote(ops: StoreOp[], info: ChangeInfo): void {
    for (const op of ops) applyOp(this, op);
    this.emit(ops, info);
  }

  // Revert local operations with their inverses, restoring the removed nodes and their proxies
  rollback(inverse: StoreOp[]): void {
    const applied = inverse.slice().reverse();
    for (const op of applied) applyOp(this, op);
    this.emit(applied, { rollback: true });
  }

  // Make the values equal to a state sent by Python, keeping the nodes and proxies of unchanged containers
  reconcile(state: JSONObject): void {
    const update = (node: StoreNode, value: any): boolean => {
      const children: any = node.children;
      const keys = Array.isArray(value) ? value.map((_, idx) => idx) : Object.keys(value);
      let changed = Array.isArray(children)
        ? children.length !== value.length
        : Object.keys(children).some((key) => !(key in value));
      if (Array.isArray(children)) children.length = Math.min(children.length, value.length);
      else for (const key of Object.keys(children)) if (!(key in value)) delete children[key];
      for (const key of keys) {
        const child = children[key];
        const next = value[key];
        const sameShape =
          child instanceof StoreNode &&
          next &&
          typeof next === "object" &&
          !isMarker(next) &&
          child.isList === Array.isArray(next);
        if (sameShape) changed = update(child, next) || changed;
        else if (child instanceof StoreNode || !sameJSON(child, next)) {
          children[key] = adopt(this, next, node);
          changed = true;
        }
      }
      // Changed nodes and their ancestors show a new snapshot
      if (changed) node.snap = undefined;
      return changed;
    };
    if (update(this.root, state)) this.emit([], { reconciled: true });
  }

  // Notify React snapshots after the current task, and subscribers with the operations under their value
  emit(ops: StoreOp[], info: ChangeInfo): void {
    for (const listener of this.listeners) queueMicrotask(listener);
    for (const subscriber of this.subscribers) {
      const path = subscriber.node.path();
      if (!path) continue;
      const events = ops
        .filter((op) => path.every((part, idx) => op.path[idx] === part) && op.path.length > path.length)
        .map((op) => ({ ...op, path: op.path.slice(path.length) }));
      if (events.length || !ops.length) subscriber.callback(events, info);
    }
  }

  listen(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  subscribe(node: StoreNode, callback: (ops: StoreOp[], info: ChangeInfo) => void): () => void {
    const subscriber = { node, callback };
    this.subscribers.add(subscriber);
    return () => this.subscribers.delete(subscriber);
  }
}

// Proxy reading and editing one map or list of a Store, compiled Python code edits Stores through it.
//
// Transcrypt lowers list.append to push, insert and remove to splice, pop to a delete and a length change,
// clear to a length change, del to delete, and dict methods to Object.prototype functions called on the proxy.
function makeProxy(node: StoreNode): any {
  const doc = node.doc;
  const editPath = (key: string | number): Path => {
    const path = node.path();
    if (!path) throw new Error("This Store value was removed from its Store");
    return [...path, key];
  };
  const listIndex = (prop: string | symbol): number | null => {
    if (typeof prop !== "string" || !/^\d+$/.test(prop)) return null;
    return Number(prop);
  };
  const list = node.children as any[];
  const map = node.children as Record<string, any>;

  const proxy = new Proxy(node.isList ? [] : {}, {
    get(_target, prop) {
      if (prop === NODE) return node;
      if (prop === JS) return node.snapshot();
      if (prop === Symbol.toStringTag) return node.isList ? "Array" : "Object";
      if (!node.isList) {
        if (typeof prop === "string" && Object.prototype.hasOwnProperty.call(map, prop)) return doc.wrap(map[prop]);
        return Reflect.get(node.snapshot(), prop, proxy);
      }
      if (prop === "length") return list.length;
      if (prop === "push") {
        return (...items: any[]) => {
          doc.transact(() => {
            for (const item of items)
              doc.edit({ op: "insert", path: editPath(list.length), value: cloneJSONValue(item) });
          });
          return list.length;
        };
      }
      if (prop === "splice") {
        return (start: number, count?: number, ...items: any[]) => {
          const size = list.length;
          start = start < 0 ? Math.max(size + start, 0) : Math.min(start, size);
          count = count === undefined ? size - start : Math.min(Math.max(count, 0), size - start);
          const removed = list.slice(start, start + count).map((child) =>
            child instanceof StoreNode ? child.snapshot() : child
          );
          doc.transact(() => {
            for (let idx = 0; idx < count; idx++) doc.edit({ op: "delete", path: editPath(start) });
            items.forEach((item, offset) =>
              doc.edit({ op: "insert", path: editPath(start + offset), value: cloneJSONValue(item) })
            );
          });
          return removed;
        };
      }
      if (prop === Symbol.iterator) {
        return function* () {
          for (let idx = 0; idx < list.length; idx++) yield doc.wrap(list[idx]);
        };
      }
      const idx = listIndex(prop);
      if (idx !== null) return idx < list.length ? doc.wrap(list[idx]) : undefined;
      return Reflect.get(node.snapshot(), prop, proxy);
    },

    set(_target, prop, value) {
      if (!node.isList) {
        if (typeof prop !== "string") return false;
        doc.edit({ op: "set", path: editPath(prop), value: cloneJSONValue(value) });
        return true;
      }
      const idx = listIndex(prop);
      if (idx !== null) {
        doc.transact(() => {
          while (list.length < idx) doc.edit({ op: "insert", path: editPath(list.length), value: null });
          if (idx === list.length) doc.edit({ op: "insert", path: editPath(idx), value: cloneJSONValue(value) });
          else doc.edit({ op: "set", path: editPath(idx), value: cloneJSONValue(value) });
        });
        return true;
      }
      if (prop === "length") {
        const size = Number(value);
        if (Number.isNaN(size)) return false;
        doc.transact(() => {
          while (list.length > size) doc.edit({ op: "delete", path: editPath(list.length - 1) });
          while (list.length < size) doc.edit({ op: "insert", path: editPath(list.length), value: null });
        });
        return true;
      }
      return false;
    },

    // Deleting a list index removes the item and shifts the following ones, as Python del does
    deleteProperty(_target, prop) {
      if (!node.isList) {
        if (typeof prop === "string" && Object.prototype.hasOwnProperty.call(map, prop))
          doc.edit({ op: "delete", path: editPath(prop) });
        return true;
      }
      const idx = listIndex(prop);
      if (idx !== null && idx < list.length) doc.edit({ op: "delete", path: editPath(idx) });
      return true;
    },

    has() {
      return true;
    },
    ownKeys() {
      return node.isList ? ["length", ...list.map((_, idx) => String(idx))] : Object.keys(map);
    },
    // Python `key in mapping` compiles to hasOwnProperty, which only finds existing keys
    getOwnPropertyDescriptor(_target, prop) {
      if (node.isList) {
        if (prop === "length") return { writable: true, enumerable: false, configurable: false };
        const idx = listIndex(prop);
        return idx !== null && idx < list.length ? { configurable: true, enumerable: true } : undefined;
      }
      return typeof prop === "string" && Object.prototype.hasOwnProperty.call(map, prop)
        ? { configurable: true, enumerable: true }
        : undefined;
    },
  });
  return proxy;
}
