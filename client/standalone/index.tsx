import React, { Suspense } from "react";
import ReactDOM from "react-dom";
import { loadPyodide } from "pyodide";
import Loading from "../components/Loading";
import DESERIALIZE_PY from "../deserialize.py";

import "@pret-globals";

import useSyncExternalStoreExports from "use-sync-external-store/shim";

// @ts-ignore
React.useSyncExternalStore = useSyncExternalStoreExports.useSyncExternalStore;

// @ts-ignore
window._empty_hook_deps = [];

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
      const Module = await container.get("./extension");
      return Module();
    })
  );
};

let pyodidePromise = null;

async function loadBundle() {
  if (!pyodidePromise) {
    pyodidePromise = loadPyodide({
      indexURL: "https://cdn.jsdelivr.net/pyodide/v0.26.2/full/",
    });
  }

  const [pyodide, bundle, extensions] = await Promise.all([
    // Load pyodide
    pyodidePromise,
    // Load the base64 bundle as a base64 string
    fetch((window as any).PRET_PICKLE_FILE).then((res) => res.text()),
    // Load the extensions that will make required modules available as globals
    loadExtensions(),
  ]);
  console.log(extensions);
  await pyodide.loadPackage("micropip");
  const micropip = pyodide.pyimport("micropip");
  await micropip.install("dill");
  (window as any).React = React;
  await pyodide.runPythonAsync(DESERIALIZE_PY);
  return { pyodide, bundle };
}

class ErrorBoundary extends React.Component {
  state: { error: any };
  props: { children: React.ReactElement };

  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    // Update state so the next render will show the fallback UI.
    return { error: error };
  }

  render() {
    if (this.state.error) {
      // You can render any custom fallback UI
      return <pre>{this.state.error.toString()}</pre>;
    }

    return this.props.children;
  }
}

const RenderBundle = ({ resource, chunkIdx }) => {
  try {
    const makeRenderable = resource.read(chunkIdx);
    return makeRenderable(chunkIdx);
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

// Initialize data-theme
document.addEventListener("DOMContentLoaded", () => {
  function updateTheme() {
    const scheme = document.body.getAttribute("data-md-color-scheme");
    let theme;
    if (scheme === "default" || scheme === null) {
      theme = "light"; // default to light if attribute is "default" or missing
    } else if (scheme === "slate") {
      theme = "dark";
    } else {
      theme = "light"; // fallback to light if any other unexpected value
    }
    // For instance, set a data attribute on the html element:
    document.documentElement.setAttribute("data-theme", theme);
  }

  // Update theme immediately on load
  updateTheme();

  // Create a MutationObserver to listen for attribute changes on the body
  const observer = new MutationObserver(mutations => {
    mutations.forEach(mutation => {
      if (mutation.attributeName === "data-md-color-scheme") {
        updateTheme();
      }
    });
  });

  // Observe changes to attributes on the body element
  observer.observe(document.body, { attributes: true });
});

function renderPret() {
  let bundlePromise = loadBundle();

  const pretChunks = document.querySelectorAll("[data-pret-chunk-idx]");

  for (let chunk of pretChunks as any) {
    const chunkIdx = parseInt(chunk.getAttribute("data-pret-chunk-idx"), 10);

    const resource = createResource(
      (async (chunkIdx) => {
        const { pyodide, bundle } = await bundlePromise;
        const locals = pyodide.toPy({ bundle_string: bundle });
        const [makeRenderable, manager] = await pyodide.runPythonAsync(
          `load_view(bundle_string, "root", ${chunkIdx})`,
          { locals: locals }
        );
        if (!makeRenderable || !manager) {
          throw new Error("Failed to unpack bundle");
        }
        return (idx) => {
          console.assert(idx === chunkIdx, "Chunk index mismatch");
          return makeRenderable();
        };
      })(chunkIdx)
    );
    ReactDOM.render(
      <React.StrictMode>
        <ErrorBoundary>
          <Suspense fallback={<Loading />}>
            <RenderBundle resource={resource} chunkIdx={chunkIdx} />
          </Suspense>
        </ErrorBoundary>
      </React.StrictMode>,
      chunk
    );
  }
}

// To support mkdocs instant navigation
// See https://squidfunk.github.io/mkdocs-material/customization/#additional-javascript
if ((window as any).document$) {
  (window as any).document$?.subscribe(function () {
    renderPret();
  });
} else {
  renderPret();
}
