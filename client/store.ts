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

type NodeRec = { y: Y.AbstractType<any>; j: JSONValue; proxy: any };

export type Store<T = any> = any;

const asJSON = (v: any) =>
  v && typeof v.toJSON === "function" ? v.toJSON() : v;

export function makeStore<T = any>(
  yRoot: Y.Map<any> | Y.Array<any>,
  initialJRoot?: JSONValue
): Store<T> {
  const cache = new WeakMap<object, NodeRec>();

  let jRoot: JSONValue = (initialJRoot ?? (yRoot as any).toJSON()) as JSONValue;

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

  function makeProxy(y: Y.AbstractType<any>, j?: JSONValue): any {
    const hit = cache.get(y as any);
    if (hit) {
      if (j !== undefined && hit.j !== j) hit.j = j;
      return hit.proxy;
    }

    const rec: NodeRec = {
      y,
      j: (j ?? (y as any).toJSON?.() ?? y) as JSONValue,
      proxy: null as any,
    };

    const isArray = Array.isArray(j);

    const proxy = new Proxy(isArray ? [] : {}, {
      get(_t, prop) {
        if (prop === NODE) return rec;
        if (prop === YJS) return y;
        if (prop === JS) return rec.j;

        if (prop === Symbol.toStringTag) {
          if (y instanceof Y.Array || Array.isArray(rec.j)) return "Array";
          if (y instanceof Y.Text || typeof rec.j === "string") return "String";
          return "Object";
        }

        if (y instanceof Y.Map) {
          const k = String(prop as any);
          const childY = y.get(k);
          const childJ = (rec.j as any)?.[k];
          if (childY instanceof Y.AbstractType) {
            if (childJ === undefined) return makeProxy(childY);
            return makeProxy(childY, childJ);
          }
          if (
            rec.j &&
            typeof rec.j === "object" &&
            Object.prototype.hasOwnProperty.call(rec.j as any, k)
          ) {
            return childJ;
          }
          return asJSON(childY); // mirror may lag while inside manual transaction
        } else if (y instanceof Y.Array) {
          if (prop === "length") {
            return y.length;
          } else if (prop === "push") {
            return (...items: any[]) => {
              const mirrorItems = items.map(cloneJSONValue);
              if (!Array.isArray(rec.j)) rec.j = [] as any;
              (rec.j as any[]).push(...mirrorItems);
              return y.doc?.transact(() => y.push(items.map(jsToY)));
            };
          } else if (prop === "splice") {
            return (start: number, del?: number, ...items: any[]) => {
              const mirrorItems = items.map(cloneJSONValue);
              if (!Array.isArray(rec.j)) rec.j = [] as any;
              if (del && del > 0) {
                (rec.j as any[]).splice(start, del, ...mirrorItems);
              } else if (mirrorItems.length) {
                (rec.j as any[]).splice(start, 0, ...mirrorItems);
              }
              return y.doc?.transact(() => {
                if (del && del > 0) y.delete(start, del);
                if (items.length) y.insert(start, items.map(jsToY));
              });
            };
          } else if (prop === Symbol.iterator) {
            return function* () {
              for (let i = 0; i < y.length; i++) {
                const childY = y.get(i);
                const childJ = rec.j[i];
                if (childY && (childY as any).toJSON) {
                  yield makeProxy(childY, childJ);
                } else {
                  yield childJ;
                }
              }
            };
          }

          const i = Number(prop);
          if (!Number.isNaN(i)) {
            const childY = y.get(i);
            const childJ = Array.isArray(rec.j)
              ? (rec.j as any[])[i]
              : undefined;
            if (childY && (childY as any).toJSON)
              return makeProxy(childY, childJ);
            if (Array.isArray(rec.j) && i < (rec.j as any[]).length) return childJ;
            return asJSON(childY); // mirror may lag while inside manual transaction
          }
        } else if (y instanceof Y.Text) {
          if (prop === "toString") return () => String(rec.j);
          if (prop === Symbol.toStringTag) return "String";
          if (prop === "length") return String(rec.j ?? "").length;
          return (rec.j as any)?.[prop as any];
        }

        return Reflect.get(rec.j as any, prop, proxy) as any;
      },

      set(_t, prop, value) {
        if (y instanceof Y.Map) {
          const k = String(prop);
          y.set(k, jsToY(value));
          if (rec.j && typeof rec.j === "object") {
            (rec.j as any)[k] = cloneJSONValue(value);
          }
          return true;
        }
        if (y instanceof Y.Array) {
          const i = Number(prop);
          if (!Number.isNaN(i)) {
            if (!Array.isArray(rec.j)) rec.j = [] as any;
            const arr = rec.j as any[];
            while (arr.length <= i) arr.push(null);
            arr[i] = cloneJSONValue(value);
            y.doc?.transact(() => {
              while (y.length <= i) y.push([null]);
              y.delete(i, 1);
              y.insert(i, [jsToY(value)]);
            });
            return true;
          } else if (prop === "length") {
            const len = Number(value);
            if (!Number.isNaN(len)) {
              if (!Array.isArray(rec.j)) rec.j = [] as any;
              const arr = rec.j as any[];
              if (len < arr.length) {
                arr.length = len;
              } else {
                while (arr.length < len) arr.push(null);
              }
              if (len < y.length) {
                y.delete(len, y.length - len);
              } else {
                y.insert(y.length, new Array(len - y.length).fill(null));
              }
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
          if (rec.j && typeof rec.j === "object") {
            delete (rec.j as any)[k];
          }
          return true;
        }
        if (y instanceof Y.Array) {
          const i = Number(prop);
          if (!Number.isNaN(i)) {
            y.delete(i, 1);
            if (Array.isArray(rec.j)) {
              (rec.j as any[]).splice(i, 1);
            }
          }
          return true;
        }
        return false;
      },

      has() {
        return true;
      },
      ownKeys() {
        if (y instanceof Y.Map) return [...y.keys()];
        if (y instanceof Y.Array)
          return ["length", ...new Array(y.length).fill(0).map((_, i) => String(i))];
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
  (yRoot as any).observeDeep((events: Array<Y.YEvent<any>>) => {
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

  const rootProxy = makeProxy(yRoot, jRoot);

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
    () => rec.j as T
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
    }
  };
  return [tx, endTransaction];
};
