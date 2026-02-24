import React, { Suspense } from "react";
import ReactDOM from "react-dom";
import { Widget as LuminoWidget } from "@lumino/widgets";

import PretJupyterHandler from "./manager";
import Loading from "../components/Loading";

export type PretSerialized = [string, string];

export type PretViewData = {
  serialized?: PretSerialized;
  marshaler_id: string;
  chunk_idx: number;
};
const PRET_READY_TIMEOUT_MS = 8000;

/**
 * A renderer for pret Views with Jupyter (Lumino) framework
 */

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

export class PretViewWidget extends LuminoWidget {
  public makeView: () => React.ReactElement;

  public manager: PretJupyterHandler;
  public keepHiddenWhenExecuted: boolean;
  model: any;
  private readonly _mimeType: string;
  private _viewData: PretViewData;
  private _isRendered: boolean;
  private _renderNode: HTMLElement;
  private _fullpageNode: HTMLDivElement | null;

  constructor(
    options: { view_data?: PretViewData; mimeType: string },
    manager: PretJupyterHandler
  ) {
    super();

    this.makeView = null;

    this._mimeType = options.mimeType;
    this._viewData = options.view_data;
    this.manager = manager;
    this.keepHiddenWhenExecuted = true;
    this._isRendered = false;
    this._renderNode = this.node;
    this._fullpageNode = null;

    this.model = null;

    // Widget will either show up "immediately", ie as soon as the manager is ready,
    // or this method will return prematurely (no view_id/view_type/model) and will
    // wait for the mimetype manager to assign a model to this view and call renderModel
    // on its own (which will call showContent)
    this.addClass("pret-view");

    this.showContent();
  }

  get viewData() {
    if (!this._viewData && this.model) {
      const source = this.model.data[this._mimeType];
      this._viewData = source["view_data"];
    }
    return this._viewData;
  }

  setFlag(flag: LuminoWidget.Flag) {
    const wasVisible = this.isVisible;
    super.setFlag(flag);
    if (this.isVisible && !wasVisible) {
      this.showContent();
    } else if (!this.isVisible && wasVisible) {
      this.hideContent();
    }
  }

  clearFlag(flag: LuminoWidget.Flag) {
    const wasVisible = this.isVisible;
    super.clearFlag(flag);
    if (this.isVisible && !wasVisible) {
      this.showContent();
    } else if (!this.isVisible && wasVisible) {
      this.hideContent();
    }
  }

  async renderModel(model) {
    this.model = model;
    this.showContent();
  }

  hideContent() {
    if (!this.isVisible && this._isRendered) {
      ReactDOM.unmountComponentAtNode(this._renderNode);
      this._isRendered = false;
    }
    if (!this.isVisible && this._fullpageNode) {
      this._fullpageNode.remove();
      this._fullpageNode = null;
      this._renderNode = this.node;
    }
  }

  dispose() {
    if (this._isRendered) {
      ReactDOM.unmountComponentAtNode(this._renderNode);
      this._isRendered = false;
    }
    if (this._fullpageNode) {
      this._fullpageNode.remove();
      this._fullpageNode = null;
    }
    super.dispose();
  }

  showContent() {
    if (!this.isVisible) {
      return;
    }

    const previousRenderNode = this._renderNode;
    const searchParams = new URLSearchParams(window.location.search);
    let fullpageCellParam = searchParams.get("pret-fullpage-cell");
    if (fullpageCellParam === null) {
      const navigationEntry = (window.performance
        ?.getEntriesByType?.("navigation")?.[0] ??
        null) as PerformanceNavigationTiming | null;
      if (navigationEntry?.name) {
        try {
          const navigationUrl = new URL(navigationEntry.name, window.location.origin);
          fullpageCellParam = navigationUrl.searchParams.get("pret-fullpage-cell");
          if (fullpageCellParam !== null) {
            searchParams.set("pret-fullpage-cell", fullpageCellParam);
            const search = searchParams.toString();
            window.history.replaceState(
              window.history.state,
              "",
              `${window.location.pathname}${search ? `?${search}` : ""}${
                window.location.hash
              }`
            );
          }
        } catch {
          // Ignore malformed URL.
        }
      }
    }
    const fullpageCell = Number.parseInt(fullpageCellParam ?? "", 10);
    let shouldUseFullpage = false;
    if (Number.isFinite(fullpageCell)) {
      const cellNode = this.node.closest(".jp-Cell") as HTMLElement | null;
      let cellIndex = Number.parseInt(
        cellNode?.dataset?.windowedListIndex ?? "",
        10
      );
      if (!Number.isFinite(cellIndex) && cellNode) {
        // jlab3 does not set windowedListIndex on cell nodes
        const notebookNode = cellNode.closest(".jp-Notebook");
        if (notebookNode) {
          cellIndex = Array.from(notebookNode.querySelectorAll(".jp-Cell")).indexOf(
            cellNode
          );
        }
      }
      if (cellNode && cellIndex === fullpageCell) {
        if (!this._fullpageNode) {
          this._fullpageNode = document.createElement("div");
          this._fullpageNode.className = "pret-fullpage-host";
          document.body.appendChild(this._fullpageNode);
        }
        shouldUseFullpage = true;
      }
    }

    if (this._isRendered) {
      ReactDOM.unmountComponentAtNode(previousRenderNode);
      this._isRendered = false;
    }

    if (shouldUseFullpage && this._fullpageNode) {
      this._renderNode = this._fullpageNode;
    } else {
      this._renderNode = this.node;
      if (this._fullpageNode) {
        this._fullpageNode.remove();
        this._fullpageNode = null;
      }
    }

    const Render = () => {
      if (!this.makeView) {
        throw Promise.race([
          this.manager.ready,
          new Promise((_, reject) => {
            setTimeout(() => {
              const error: any = new Error(
                "Timed out while connecting to the Jupyter backend."
              );
              error.code = "PRET_JUPYTER_DOWN";
              reject(error);
            }, PRET_READY_TIMEOUT_MS);
          }),
        ]).then(async () => {
          try {
            const unpackOnce = async () => {
              const viewData = await this.manager.resolveViewData(this.viewData);
              this.makeView = this.manager.unpackView(viewData);
            };

            try {
              await unpackOnce();
            } catch (e: any) {
              const message = String(e?.message ?? e);
              const retriable =
                Boolean(e?.incomplete) ||
                message.includes("Unexpected end of CBOR") ||
                message.includes("InvalidCharacterError") ||
                message.includes("chunkIdx") ||
                message.includes("Bundle response");

              if (retriable && this.viewData?.marshaler_id) {
                // Clear cache and retry once
                this.manager.clearBundleCache(this.viewData.marshaler_id);
                this.manager.clearUnpackCache(this.viewData.marshaler_id);
                await unpackOnce();
              } else {
                throw e;
              }
            }
          } catch (e) {
            console.error(e);
            const message = String(e?.message ?? e);
            const code = String((e as any)?.code ?? "");
            const kernelDown =
              code === "PRET_KERNEL_DOWN" ||
              code === "PRET_STALE_MARSHALER" ||
              code === "PRET_BUNDLE_TIMEOUT" ||
              message.includes("not found in current session");
            const jupyterDown = code === "PRET_JUPYTER_DOWN";
            const useFullpageFallback =
              this._fullpageNode !== null && this._renderNode === this._fullpageNode;
            this.makeView = () => {
              const [isRestarting, setIsRestarting] = React.useState(false);
              const [restartError, setRestartError] = React.useState("");
              return (
                <div
                  className={
                    useFullpageFallback
                      ? "pret-fullpage-fallback"
                      : "pret-inline-fallback"
                  }
                >
                  <h3 style={{ margin: 0 }}>
                    {jupyterDown
                      ? "Jupyter backend is unavailable"
                      : kernelDown
                        ? "Kernel is out of sync with this PRET widget"
                        : "Failed to load PRET widget"}
                  </h3>
                  <p style={{ margin: 0 }}>
                    {jupyterDown
                      ? "Cannot load the PRET bundle because the Jupyter server connection is not available."
                      : kernelDown
                        ? "Restart the kernel and run all cells to rebuild the app state for this page."
                        : "An unexpected error occurred while loading the PRET app."}
                  </p>
                  {!jupyterDown && kernelDown && (
                    <button
                      className="pret-restart-app-button"
                      disabled={isRestarting}
                      onClick={async () => {
                        setRestartError("");
                        setIsRestarting(true);
                        try {
                          const restart = (window as any).pretRestartAndRunAll;
                          if (typeof restart !== "function") {
                            throw new Error(
                              "Restart command is not available in this Jupyter session."
                            );
                          }
                          await restart();
                          this.makeView = null;
                          this.showContent();
                        } catch (restartErr: any) {
                          setRestartError(String(restartErr?.message ?? restartErr));
                        } finally {
                          setIsRestarting(false);
                        }
                      }}
                    >
                      {isRestarting ? "Restarting..." : "Restart the app"}
                    </button>
                  )}
                  {!jupyterDown && !kernelDown && (
                    <code>{message}</code>
                  )}
                  {restartError && <code>{restartError}</code>}
                </div>
              );
            };
          }
        });
      }
      return this.makeView();
    };

    ReactDOM.render(
      <ErrorBoundary>
        <Suspense fallback={<Loading />}>
          <Render />
        </Suspense>
      </ErrorBoundary>,
      this._renderNode
    );

    this._isRendered = true;
  }
}
