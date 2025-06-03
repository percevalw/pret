import React, { Suspense } from "react";
import ReactDOM from "react-dom";
import Loading from "../components/Loading";

import "@pret-globals";

import { makeLoadApp } from "../appLoader";

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

async function loadBundle() {
  const [bundle, extensions] = await Promise.all([
    fetch((window as any).PRET_PICKLE_FILE).then((res) => res.json()),
    // Load the extensions that will make required modules available as globals
    loadExtensions(),
  ]);
  return {
    bundle: bundle,
  }
}

class ErrorBoundary extends React.Component {
  state: { error: any };
  props: { children: React.ReactElement };

  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error: error };
  }

  render() {
    if (this.state.error) {
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
    if (err instanceof Promise) {
      throw err;
    } else {
      console.error(err);
      return <div>Error: {err.message}</div>;
    }
  }
};

document.addEventListener("DOMContentLoaded", () => {
  function updateTheme() {
    const scheme = document.body.getAttribute("data-md-color-scheme");
    let theme;
    if (scheme === "default" || scheme === null) {
      theme = "light";
    } else if (scheme === "slate") {
      theme = "dark";
    } else {
      theme = "light";
    }
    document.documentElement.setAttribute("data-theme", theme);
  }

  updateTheme();

  const observer = new MutationObserver(mutations => {
    mutations.forEach(mutation => {
      if (mutation.attributeName === "data-md-color-scheme") {
        updateTheme();
      }
    });
  });

  observer.observe(document.body, { attributes: true });
});

function renderPret() {
  const bundlePromise = loadBundle();
  const loadApp = makeLoadApp();

  const pretChunks = document.querySelectorAll("[data-pret-chunk-idx]");

  for (let chunk of pretChunks as any) {
    const chunkIdx = parseInt(chunk.getAttribute("data-pret-chunk-idx"), 10);

    const resource = createResource(
      (async (chunkIdx) => {
        const { bundle } = await bundlePromise;
        const [makeRenderable, manager] = loadApp(bundle, "root", chunkIdx);
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
