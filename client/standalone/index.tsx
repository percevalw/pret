import React, { Suspense } from "react";
import ReactDOM from "react-dom";
import { loadPyodide } from "pyodide";
import DESERIALIZE_PY from "../deserialize.py";

import "@pret-globals";

import useSyncExternalStoreExports from "use-sync-external-store/shim";

// @ts-ignore
React.useSyncExternalStore = useSyncExternalStoreExports.useSyncExternalStore;

// @ts-ignore
window._empty_hook_deps = [];

// @ts-ignore
let makeRenderable = null;
let manager = null;

const createResource = (promise) => {
  let status = "loading";
  let result = promise.then(
    (resolved) => {
      status = "success";
      result = resolved;
    },
    (rejected) => {
      status = "error";
      result = rejected;
    }
  );
  return {
    read() {
      if (status === "loading") {
        throw result;
      } else if (status === "error") {
        throw result;
      } else {
        return result;
      }
    },
  };
};

declare const __webpack_init_sharing__: (shareScope: string) => Promise<void>;
declare const __webpack_share_scopes__: { default: string };

const loadExtensions = async () => {
  return Promise.all(
    (window as any).PRET_REMOTE_IMPORTS.map(async (path) => {
      await __webpack_init_sharing__("default");
      const container = (window as any)._JUPYTERLAB[path];
      await container.init(__webpack_share_scopes__.default);
      const Module = await container.get("./index");
      return Module();
    })
  );
};

const ready = createResource(
  (async () => {
    const [pyodide, bundle, extensions] = await Promise.all([
      // Load pyodide
      loadPyodide({
        indexURL: "https://cdn.jsdelivr.net/pyodide/v0.23.2/full/",
      }),
      // Load the base64 bundle as a base64 string
      fetch((window as any).PRET_PICKLE_FILE).then((res) => res.text()),
      // Load the extensions that will make required modules available as globals
      loadExtensions(),
    ]);
    console.log(extensions);
    await pyodide.loadPackage("micropip");
    const micropip = pyodide.pyimport("micropip");
    await micropip.install("dill");
    window.React = React;
    const deserialize = pyodide.runPython(DESERIALIZE_PY);
    [makeRenderable, manager] = deserialize(bundle, "root", 0);
    if (!makeRenderable || !manager) {
      throw new Error("Failed to unpack bundle");
    }
    return makeRenderable;
  })()
);

const RenderBundle = () => {
  try {
    const makeRenderable = ready.read();
    return makeRenderable();
  } catch (err) {
    // If it's still loading, we throw err to be caught by Suspense
    if (err instanceof Promise) {
      throw err;
    } else {
      // This means we got an actual error
      console.error(err);
      return <div>Error: {err.message}</div>;
    }
  }
};

const rootDiv = document.createElement("div");
rootDiv.id = "root";
document.body.appendChild(rootDiv);

ReactDOM.render(
  <React.StrictMode>
    <Suspense fallback={"Loading"}>
      <RenderBundle />
    </Suspense>
  </React.StrictMode>,
  rootDiv
);
