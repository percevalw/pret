import React, { Suspense } from "react";
import ReactDOM from "react-dom";
import { Widget as LuminoWidget } from "@lumino/widgets";

import PretJupyterHandler from "./manager";
import Loading from "../components/Loading";

export type PretViewData = {
  serialized: string;
  marshaler_id: string;
  chunk_idx: number;
};

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
      ReactDOM.unmountComponentAtNode(this.node);
      this._isRendered = false;
    }
  }

  showContent() {
    if (!this.isVisible) {
      return;
    }

    if (this._isRendered) {
      ReactDOM.unmountComponentAtNode(this.node);
      this._isRendered = false;
    }

    const Render = () => {
      if (!this.makeView) {
        throw this.manager.ready.then(() => {
          try {
            this.makeView = this.manager.unpackView(this.viewData);
          } catch (e) {
            console.error(e);
            this.makeView = () => <code>{e.toString()}</code>;
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
      this.node
    );

    this._isRendered = true;
  }
}
