import React from "react";
import { addExtension, Decoder } from "./cborDecoder";
import * as valtio from "valtio";
import * as Y from "yjs";
import { bind } from "./vendor/valtio-yjs";

import { createProxy, getUntracked, trackMemo } from "proxy-compare";
import useSyncExternalStoreExports from "use-sync-external-store/shim";
import * as pyRuntime from "./org.transcrypt.__runtime__";

(window as any).pret_modules = (window as any).pret || {};
(window as any).pret_modules["org.transcrypt.__runtime__"] = pyRuntime;

Object.defineProperty(Y.Doc.prototype, "on_update", {
  value(callback: (update: Uint8Array) => void) {
    this.on("update", callback);
  },
  configurable: true,
  enumerable: false,
});

Object.defineProperty(Y.Doc.prototype, "apply_update", {
  value(update: Uint8Array) {
    Y.applyUpdate(this, update);
  },
  configurable: true,
  enumerable: false,
});

// Put the global variables in the window object to allow pret stub components and stub functions to access them
(window as any).React = React;
(React as any).useSyncExternalStore =
  useSyncExternalStoreExports.useSyncExternalStore;
(window as any).valtio = valtio;
(valtio as any).createProxy = createProxy;
(valtio as any).getUntracked = getUntracked;
(valtio as any).trackMemo = trackMemo;
(valtio as any).bind = bind;
(window as any).Y = Y;

// TODO: should this be in the scope of loadApp?
const factories = {};

// standard factory decoder
addExtension({
  tag: 4000,
  Class: null,
  encode: null,
  decode([factoryName, closureArgs]: [string, any[]]) {
    return factories[factoryName](...closureArgs);
  },
});

// class decoder
addExtension({
  tag: 4001,
  Class: null,
  encode: null,
  decode([name, bases, non_methods, methods]) {
    // @ts-ignore
    const cls = { ...non_methods };
    Object.defineProperties(
      cls,
      Object.fromEntries(
        Object.entries(methods).map(([key, fn]) => [
          key,
          {
            get: function () {
              return pyRuntime.__get__(this, fn);
            },
            enumerable: true,
            configurable: true,
          },
        ])
      )
    );
    return pyRuntime._class_(name, bases, cls);
  },
});

// __reduce__ decoder
addExtension({
  tag: 4002,
  Class: null,
  encode: null,
  decode([reconstructFn, args]) {
    return reconstructFn(...args);
  },
});

// instance decoder
addExtension({
  tag: 4003,
  Class: null,
  encode: null,
  decode([cls, dict]) {
    // @ts-ignore
    const instance = cls.__new__(/* should be called wipytth cls, TODO*/);
    // assign all properties from dict to instance
    for (const [key, value] of Object.entries(dict)) {
      instance[key] = value;
    }
    return instance;
  },
});

export function makeLoadApp() {
  const cache = new Map();

  return function loadApp(serialized, marshalerId, chunkIdx) {
    if (!cache.has(marshalerId)) {
      let cached = [
        new Decoder({ useRecords: false, shareReferenceMap: true } as any),
        new Map(),
        0,
      ];
      cache.set(marshalerId, cached);
    }
    let [decoder, chunkStore, lastOffset] = cache.get(marshalerId);

    if (chunkStore.has(chunkIdx)) {
      return chunkStore.get(chunkIdx);
    }

    const [cborDataB64, code] = serialized;
    Object.assign(factories, new Function(code)());

    const bytes = Uint8Array.from(atob(cborDataB64).slice(lastOffset), (c) =>
      c.charCodeAt(0)
    );
    const results = decoder.decodeMultiple(bytes);
    let nextIdx = chunkStore.size;
    for (const obj of results) {
      chunkStore.set(nextIdx++, obj);
    }
    cache.get(marshalerId)[2] += bytes.length;

    if (!chunkStore.has(chunkIdx)) {
      throw new RangeError(
        `Decoded ${chunkStore.size} objects, but chunkIdx ${chunkIdx} was not found.`
      );
    }
    return chunkStore.get(chunkIdx);
  };
}
