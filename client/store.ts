import * as Y from "yjs";
import { useSyncExternalStore } from "react";
import { Transaction } from "yjs";

type JSONPrimitive = string | number | boolean | null;
export type JSONValue = JSONPrimitive | JSONObject | JSONArray;

export interface JSONObject {
  [k: string]: JSONValue;
}

export type JSONArray = JSONValue[];

type Path = (string | number)[];
const JS = Symbol("js");
const YJS = Symbol("yjs");
const NODE = Symbol("node");

type StoreState = {
  beginDraft(tx: Y.Transaction): void;
  finalizeDraft(tx: Y.Transaction): void;
  readCommitted(path: Path): JSONValue;
  readVisible(path: Path): JSONValue;
};

type NodeRec = {
  y: Y.AbstractType<any>;
  j: JSONValue;
  path: Path;
  proxy: any;
  state: StoreState;
};

export type Store<T = any> = any;

const asJSON = (v: any) =>
  v && typeof v.toJSON === "function" ? v.toJSON() : v;

const undoManagersByDoc = new WeakMap<Y.Doc, Y.UndoManager>();

const getUndoScope = (doc: Y.Doc): Y.AbstractType<any>[] => {
  const scope: Y.AbstractType<any>[] = [];
  const share = (doc as any).share;
  if (share && typeof share.forEach === "function") {
    share.forEach((value: any) => {
      if (value instanceof Y.AbstractType) {
        scope.push(value);
      }
    });
  }
  if (!scope.length) {
    scope.push(doc.getMap("_"));
  }
  return scope;
};

const getDocFromProxy = (proxy: any): Y.Doc => {
  const rec: NodeRec | undefined = proxy?.[NODE];
  if (!rec) {
    throw new Error("Expected a proxy created by makeStore.");
  }
  const doc = rec.y.doc;
  if (!doc) {
    throw new Error("Expected a proxy bound to an attached Y.Doc.");
  }
  return doc;
};

export function ensureUndoManagerForDoc(doc: Y.Doc): Y.UndoManager {
  let manager = undoManagersByDoc.get(doc);
  if (!manager) {
    manager = new Y.UndoManager(getUndoScope(doc), { captureTimeout: 0 });
    undoManagersByDoc.set(doc, manager);
  }
  return manager;
}

export function ensureUndoManager(proxy: any): Y.UndoManager {
  return ensureUndoManagerForDoc(getDocFromProxy(proxy));
}

export function undoDoc(doc: Y.Doc): boolean {
  const manager = ensureUndoManagerForDoc(doc);
  if (!manager.canUndo()) {
    return false;
  }
  manager.undo();
  return true;
}

export function redoDoc(doc: Y.Doc): boolean {
  const manager = ensureUndoManagerForDoc(doc);
  if (!manager.canRedo()) {
    return false;
  }
  manager.redo();
  return true;
}

export function makeStore<T = any>(
  yRoot: Y.Map<any> | Y.Array<any>,
  initialJRoot?: JSONValue
): Store<T> {
  const cache = new WeakMap<object, NodeRec>();

  let jRoot: JSONValue = (initialJRoot ?? (yRoot as any).toJSON()) as JSONValue;
  let draftTx: Y.Transaction | null = null;
  let draftRoot: JSONValue | null = null;
  let draftDetached = new WeakSet<object>();
  const suppressedMirrorTransactions = new WeakSet<Y.Transaction>();

  const cloneJSONValue = (value: any): JSONValue => {
    const snap = value?.[JS] ?? value;
    if (Array.isArray(snap)) {
      return snap.map(cloneJSONValue) as JSONArray;
    }
    if (snap && typeof snap === "object") {
      const out: JSONObject = {};
      for (const [k, v] of Object.entries(snap)) out[k] = cloneJSONValue(v);
      return out;
    }
    return snap as JSONPrimitive;
  };

  const isContainer = (value: any): value is JSONObject | JSONArray =>
    Array.isArray(value) || (!!value && typeof value === "object");

  const shallowCloneContainer = (
    value: any,
    nextPathPart?: string | number
  ): JSONObject | JSONArray => {
    if (Array.isArray(value)) return value.slice();
    if (value && typeof value === "object") return { ...value };
    return typeof nextPathPart === "number" ? [] : {};
  };

  const readAtPath = (root: any, path: Path): any => {
    let current = root;
    for (const part of path) {
      if (current == null) return undefined;
      current = current[part as any];
    }
    return current;
  };

  const readCommitted = (path: Path): JSONValue => readAtPath(jRoot, path) as JSONValue;

  const readVisible = (path: Path): JSONValue => {
    const root = draftTx ? (draftRoot ?? jRoot) : jRoot;
    return readAtPath(root, path) as JSONValue;
  };

  const ensureWritableDraftContainer = (path: Path): any => {
    if (!draftTx) return null;

    if (draftRoot === null) {
      draftRoot = jRoot;
      draftDetached = new WeakSet<object>();
    }

    if (!isContainer(draftRoot) || !draftDetached.has(draftRoot as object)) {
      draftRoot = shallowCloneContainer(draftRoot, path[0]) as JSONValue;
      if (isContainer(draftRoot)) draftDetached.add(draftRoot as object);
    }

    let current: any = draftRoot;
    for (let i = 0; i < path.length; i++) {
      const part = path[i];
      const nextPart = path[i + 1];
      let child = current?.[part as any];

      if (!isContainer(child)) {
        child = shallowCloneContainer(child, nextPart);
        current[part as any] = child;
        if (isContainer(child)) draftDetached.add(child as object);
      } else if (!draftDetached.has(child as object)) {
        const clonedChild = shallowCloneContainer(child, nextPart);
        current[part as any] = clonedChild;
        child = clonedChild;
        draftDetached.add(child as object);
      }

      current = child;
    }

    return current;
  };

  const patchDraft = (rec: NodeRec, mutator: (container: any) => void) => {
    if (!draftTx) return;
    const container = ensureWritableDraftContainer(rec.path);
    if (!isContainer(container)) return;
    mutator(container);
  };

  const refreshArrayChildPaths = (arrayY: Y.Array<any>, basePath: Path) => {
    for (let i = 0; i < arrayY.length; i++) {
      const childY = arrayY.get(i);
      if (childY instanceof Y.AbstractType) {
        const childRec = cache.get(childY as any);
        if (childRec) childRec.path = [...basePath, i];
      }
    }
  };

  const refreshMapChildPaths = (mapY: Y.Map<any>, basePath: Path) => {
    for (const [k, childY] of mapY.entries()) {
      if (childY instanceof Y.AbstractType) {
        const childRec = cache.get(childY as any);
        if (childRec) childRec.path = [...basePath, k];
      }
    }
  };

  const state: StoreState = {
    beginDraft(tx: Y.Transaction) {
      draftTx = tx;
      draftRoot = jRoot;
      draftDetached = new WeakSet<object>();
      suppressedMirrorTransactions.add(tx);
    },
    finalizeDraft(tx: Y.Transaction) {
      try {
        if (draftTx === tx && draftRoot !== null) {
          jRoot = draftRoot;
          const rootRec = cache.get(yRoot as any);
          if (rootRec) rootRec.j = jRoot;
        }
      } finally {
        if (draftTx === tx) {
          draftTx = null;
          draftRoot = null;
          draftDetached = new WeakSet<object>();
        }
        suppressedMirrorTransactions.delete(tx);
      }
    },
    readCommitted,
    readVisible,
  };

  function makeProxy(y: Y.AbstractType<any>, j?: JSONValue, path: Path = []): any {
    const hit = cache.get(y as any);
    if (hit) {
      if (j !== undefined && hit.j !== j) hit.j = j;
      hit.path = path;
      return hit.proxy;
    }

    const rec: NodeRec = {
      y,
      j: (j ?? (y as any).toJSON?.() ?? y) as JSONValue,
      path,
      proxy: null as any,
      state,
    };

    const isArray = Array.isArray(j ?? rec.j);

    const proxy = new Proxy(isArray ? [] : {}, {
      get(_t, prop) {
        if (prop === NODE) return rec;
        if (prop === YJS) return y;
        if (prop === JS) return readVisible(rec.path);

        const currentJ = readVisible(rec.path) as any;

        if (prop === Symbol.toStringTag) {
          if (y instanceof Y.Array || Array.isArray(currentJ)) return "Array";
          if (y instanceof Y.Text || typeof currentJ === "string") return "String";
          return "Object";
        }

        if (y instanceof Y.Map) {
          if (typeof prop !== "string" && typeof prop !== "number") {
            return Reflect.get(currentJ as any, prop, proxy) as any;
          }
          const k = String(prop as any);
          const childY = y.get(k);
          const childJ = currentJ?.[k];
          if (childY instanceof Y.AbstractType) {
            return makeProxy(childY, childJ, [...rec.path, k]);
          }
          return childJ;
        } else if (y instanceof Y.Array) {
          if (prop === "length") {
            return Array.isArray(currentJ) ? currentJ.length : y.length;
          } else if (prop === "push") {
            return (...items: any[]) => {
              const result = y.doc?.transact(() => y.push(items.map(jsToY)));
              patchDraft(rec, (container) => {
                if (!Array.isArray(container)) return;
                container.push(...items.map(cloneJSONValue));
              });
              refreshArrayChildPaths(y, rec.path);
              return result;
            };
          } else if (prop === "splice") {
            return (start: number, del?: number, ...items: any[]) => {
              const result = y.doc?.transact(() => {
                if (del && del > 0) y.delete(start, del);
                if (items.length) y.insert(start, items.map(jsToY));
              });
              patchDraft(rec, (container) => {
                if (!Array.isArray(container)) return;
                const mapped = items.map(cloneJSONValue);
                if (del && del > 0) {
                  container.splice(start, del, ...mapped);
                } else if (mapped.length) {
                  container.splice(start, 0, ...mapped);
                }
              });
              refreshArrayChildPaths(y, rec.path);
              return result;
            };
          } else if (prop === Symbol.iterator) {
            return function* () {
              const len = (proxy as any).length;
              for (let i = 0; i < len; i++) {
                yield (proxy as any)[i];
              }
            };
          }

          const i = Number(prop);
          if (!Number.isNaN(i)) {
            const childY = y.get(i);
            const childJ = Array.isArray(currentJ) ? currentJ[i] : undefined;
            if (childY && (childY as any).toJSON)
              return makeProxy(childY, childJ, [...rec.path, i]);
            return childJ;
          }
        } else if (y instanceof Y.Text) {
          if (prop === "toString") return () => String(currentJ);
          if (prop === Symbol.toStringTag) return "String";
          if (prop === "length") return String(currentJ ?? "").length;
          return (currentJ as any)?.[prop as any];
        }

        return Reflect.get(currentJ as any, prop, proxy) as any;
      },

      set(_t, prop, value) {
        if (y instanceof Y.Map) {
          const k = String(prop);
          y.set(k, jsToY(value));
          patchDraft(rec, (container) => {
            if (Array.isArray(container) || !container) return;
            (container as any)[k] = cloneJSONValue(value);
          });
          refreshMapChildPaths(y, rec.path);
          return true;
        }
        if (y instanceof Y.Array) {
          const i = Number(prop);
          if (!Number.isNaN(i)) {
            y.doc?.transact(() => {
              while (y.length <= i) y.push([null]);
              y.delete(i, 1);
              y.insert(i, [jsToY(value)]);
            });
            patchDraft(rec, (container) => {
              if (!Array.isArray(container)) return;
              while (container.length <= i) container.push(null);
              container[i] = cloneJSONValue(value);
            });
            refreshArrayChildPaths(y, rec.path);
            return true;
          } else if (prop === "length") {
            const len = Number(value);
            if (!Number.isNaN(len)) {
              if (len < y.length) {
                y.delete(len, y.length - len);
              } else {
                y.insert(y.length, new Array(len - y.length).fill(null));
              }
              patchDraft(rec, (container) => {
                if (!Array.isArray(container)) return;
                if (len < container.length) {
                  container.length = len;
                } else {
                  while (container.length < len) container.push(null);
                }
              });
              refreshArrayChildPaths(y, rec.path);
              return true;
            }
          }
        }
        return false;
      },

      deleteProperty(_t, prop) {
        if (y instanceof Y.Map) {
          const k = String(prop);
          y.delete(k);
          patchDraft(rec, (container) => {
            if (Array.isArray(container) || !container) return;
            delete (container as any)[k];
          });
          refreshMapChildPaths(y, rec.path);
          return true;
        }
        if (y instanceof Y.Array) {
          const i = Number(prop);
          if (!Number.isNaN(i)) {
            y.delete(i, 1);
            patchDraft(rec, (container) => {
              if (!Array.isArray(container)) return;
              container.splice(i, 1);
            });
            refreshArrayChildPaths(y, rec.path);
          }
          return true;
        }
        return false;
      },

      has() {
        return true;
      },
      ownKeys() {
        const currentJ = readVisible(rec.path) as any;
        if (y instanceof Y.Map && currentJ && typeof currentJ === "object") {
          return Object.keys(currentJ);
        }
        if (y instanceof Y.Array && Array.isArray(currentJ)) {
          return ["length", ...currentJ.map((_: any, i: number) => String(i))];
        }
        return [];
      },
      getOwnPropertyDescriptor(target, name) {
        if (y instanceof Y.Array) {
          if (name === "length") {
            return {
              writable: true,
              enumerable: false,
              configurable: false,
            };
          }
          return {
            configurable: true,
            enumerable: true,
          };
        } else {
          return {
            enumerable: true,
            configurable: true,
          };
        }
      },
    });

    rec.proxy = proxy;
    cache.set(y as any, rec);
    return proxy;
  }

  // Deep observer that clones along changed paths and rewires proxies in-place.
  (yRoot as any).observeDeep((events: Array<Y.YEvent<any>>, transaction: Y.Transaction) => {
    if (suppressedMirrorTransactions.has(transaction)) return;
    if (!events.length) return;

    // Sort by path (lexicographic) to maximize LCP reuse
    const pathStr = (p: Path) => p.map(String).join("\x1f");
    events.sort((a, b) =>
      pathStr((a as any).path).localeCompare(pathStr((b as any).path))
    );

    const cloned = new Set<object>();

    for (const ev of events) {
      const path = ev.path as Path;

      let currentJ: JSONValue = jRoot;
      let currentY = yRoot;
      let currentPath: Path = [];

      const maybeCloneCurrentAndRewire_ = () => {
        if (cloned.has(currentY)) return currentJ;
        if (Array.isArray(currentJ)) {
          currentJ = currentJ.slice();
        } else if (typeof currentJ === "object") {
          currentJ = { ...currentJ };
        }
        const rec = cache.get(currentY);
        if (rec) {
          if (rec.j === currentJ) {
            throw new Error(
              "Rewiring to the same JSON value, this should not happen."
            );
          }
          rec.j = currentJ; // rewire to new JSON value
          rec.path = currentPath;
        }
        return currentJ;
      };
      maybeCloneCurrentAndRewire_();
      jRoot = currentJ; // always update rootJ to the current JSON value

      // for part of path
      for (let prop of path) {
        const parentJ = currentJ;
        if (currentY instanceof Y.Map) {
          currentY = currentY.get(prop as string);
          currentJ = currentJ[prop as keyof typeof currentJ];
        } else if (currentY instanceof Y.Array) {
          const i = Number(prop);
          currentY = currentY.get(i);
          currentJ = Array.isArray(currentJ) ? currentJ[i] : undefined;
        }
        currentPath = [...currentPath, prop as any];
        maybeCloneCurrentAndRewire_();
        parentJ[prop] = currentJ;
      }

      if (ev.target instanceof Y.Map) {
        const yEv = ev as Y.YMapEvent<any>;

        yEv.changes.keys.forEach((info, k) => {
          if (info.action === "delete") {
            delete (currentJ as any)[k];
          } else {
            const childY = (ev.target as Y.Map<any>).get(k);
            const value = asJSON(childY);
            (currentJ as any)[k] = value;
            if (cache.has(childY) as any) {
              const r = cache.get(childY as any);
              if (r) r.j = value;
            }
          }
        });
        refreshMapChildPaths(ev.target as Y.Map<any>, path);
      }
      // Handle Y.Array events
      else if (ev.target instanceof Y.Array) {
        const yEv = ev as Y.YArrayEvent<any>;

        // Ensure currentJ is an array we can mutate (it should already be a cloned array)
        if (!Array.isArray(currentJ)) {
          currentJ = [] as any[];
          const rec = cache.get(ev.target as any);
          if (rec) rec.j = currentJ;
        }

        let idx = 0;
        for (const d of yEv.changes.delta) {
          if ((d as any).retain) {
            idx += (d as any).retain as number;
            continue;
          }
          if ((d as any).insert) {
            const inserts = (d as any).insert as any[];
            const mapped = inserts.map((it) => asJSON(it));
            (currentJ as any[]).splice(idx, 0, ...mapped);

            // Rewire proxies for inserted Y types (if any)
            for (let k = 0; k < inserts.length; k++) {
              const ins = inserts[k];
              if (
                ins &&
                typeof ins === "object" &&
                typeof (ins as any).toJSON === "function"
              ) {
                const r = cache.get(ins as any);
                if (r) r.j = mapped[k];
              }
            }
            idx += mapped.length;
            continue;
          }
          if ((d as any).delete) {
            const del = (d as any).delete as number;
            (currentJ as any[]).splice(idx, del);
            continue;
          }
        }
        refreshArrayChildPaths(ev.target as Y.Array<any>, path);
      }
    }
  });

  function jsToY(value: any): any {
    if (Array.isArray(value)) {
      const a = new Y.Array<any>();
      a.push(value.map(jsToY));
      return a;
    }
    if (value && typeof value === "object") {
      const m = new Y.Map<any>();
      for (const [k, v] of Object.entries(value)) m.set(k, jsToY(v));
      return m;
    }
    return value;
  }

  const rootProxy = makeProxy(yRoot, jRoot, []);

  return rootProxy as Store<T>;
}

export function snapshot(proxy: any): JSONValue {
  // If proxy is indeed a proxy return proxy.j, otherwise return the obj.
  return proxy?.[JS] || proxy;
}

export function subscribe(
  proxy: any,
  cb: (arg0: Y.YEvent<any>[], arg1: Y.Transaction) => void,
  notifyInSync: boolean
): () => void {
  const rec: NodeRec | undefined = proxy?.[NODE];
  if (!rec) throw new Error("subscribe expects a proxy created by makeStore.");
  const y = rec.y as Y.AbstractType<any>;
  if (notifyInSync) {
    y.observeDeep(cb);
  } else {
    // TODO: maybe queue the events and call the callback with all events at once?
    y.observeDeep((events: Y.YEvent<any>[], transaction: Y.Transaction) => {
      queueMicrotask(() => cb(events, transaction));
    });
  }
  return () => {
    y.unobserveDeep(cb);
  };
}

export function useSnapshot<T = any>(node: any): T {
  const rec: NodeRec | undefined = node?.[NODE];
  if (!rec)
    throw new Error("useSnapshot expects a proxy created by makeStore.");

  return useSyncExternalStore(
    (cb) => {
      const handler = () => {
        queueMicrotask(cb);
      }; // ensure deep observer rewiring happened
      (rec.y as any).observeDeep(handler);
      return () => (rec.y as any).unobserveDeep(handler);
    },
    () => rec.state.readCommitted(rec.path) as T
  );
}


export const beginTransaction = (
  proxy: any,
  origin: any = null,
  local: boolean = true
): [Transaction, () => void] => {
  const rec: NodeRec | undefined = proxy?.[NODE];
  if (!rec)
    throw new Error("beginTransaction expects a proxy created by makeStore.");

  type DocInternals = Y.Doc & {
    _transaction: Y.Transaction | null;
    _transactionCleanups: Y.Transaction[];
    emit: (eventName: string, args: any[]) => void;
  };

  const doc = rec.y.doc as DocInternals | null;
  if (!doc)
    throw new Error("beginTransaction expects a proxy bound to an attached Y.Doc.");

  // Nested manual begin inside an existing transaction does not create a new scope in Yjs.
  if (doc._transaction !== null) {
    return [doc._transaction, () => {}];
  }

  const tx = new Transaction(doc, origin, local);
  const transactionCleanups = doc._transactionCleanups;

  doc._transaction = tx;
  transactionCleanups.push(tx);
  if (transactionCleanups.length === 1) {
    doc.emit("beforeAllTransactions", [doc]);
  }
  doc.emit("beforeTransaction", [tx, doc]);
  rec.state.beginDraft(tx);

  let ended = false;
  const endTransaction = () => {
    if (ended) return;
    ended = true;

    if (doc._transaction !== tx) {
      throw new Error("endTransaction called with a non-active transaction.");
    }

    // Force Yjs to run its internal cleanupTransactions(...) for our transaction.
    // We do this calling Y.transact, then swapping its new transaction with our
    // current, manually instantiated tx transaction, so that it cleans it up
    // instead.
    doc._transaction = null;
    let preventBeforeTransactionEmission = true;
    const originalEmit = doc.emit;
    doc.emit = ((eventName: string, args: any[]) => {
      if (eventName === "beforeTransaction" && preventBeforeTransactionEmission) {
        preventBeforeTransactionEmission = false;
        return;
      }
      return originalEmit.call(doc, eventName, args);
    }) as DocInternals["emit"];

    try {
      Y.transact(
        doc,
        (internalTx: Y.Transaction) => {
          const cleanups = doc._transactionCleanups;
          if (cleanups[cleanups.length - 1] === internalTx) {
            cleanups.pop();
          }
          doc._transaction = tx;
        },
        origin,
        local
      );
    } finally {
      doc.emit = originalEmit;
      rec.state.finalizeDraft(tx);
    }
  };
  return [tx, endTransaction];
};
