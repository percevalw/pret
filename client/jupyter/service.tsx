import React, { Suspense } from "react";
import ReactDOM from "react-dom";
import { Token } from "@lumino/coreutils";
import type { ISessionContext } from "@jupyterlab/apputils";
import type { ServiceManager } from "@jupyterlab/services";

import PretJupyterHandler from "./manager";
import type { PretViewData } from "./widget";

export interface IPretHost {
  render(viewReference: PretViewData): Promise<void>;
  dispose(): void;
}

export interface IPretService {
  createHost(sessionContext: ISessionContext, node: HTMLElement): IPretHost;
}

export const IPretService = new Token<IPretService>("pret:service");

const managers = new WeakMap<ISessionContext, PretJupyterHandler>();

// Reuse the same Jupyter connection for notebook outputs and extension views
export function getPretManager(
  sessionContext: ISessionContext,
  serviceManager: ServiceManager.IManager
): PretJupyterHandler {
  let manager = managers.get(sessionContext);
  if (!manager) {
    manager = new PretJupyterHandler(sessionContext, serviceManager);
    managers.set(sessionContext, manager);
    sessionContext.disposed.connect(() => {
      manager?.dispose();
      managers.delete(sessionContext);
    });
  }
  return manager;
}

class PretHost implements IPretHost {
  private renderVersion = 0;
  private release: (() => void) | null;

  constructor(private manager: PretJupyterHandler, private node: HTMLElement) {
    this.release = manager.acquire();
  }

  // Fetch the Python view definition before rendering the Jupyter extension view
  async render(viewReference: PretViewData): Promise<void> {
    if (!this.release) return;
    const version = ++this.renderVersion;
    const started = performance.now();
    // Keep the view identifiers on each stage so extension logs can follow concurrent openings
    const reportStage = (stage: string) => console.info("PRET: Host render progress", {
      stage,
      marshalerId: viewReference.marshaler_id,
      chunkIndex: viewReference.chunk_idx,
      elapsedMs: Math.round(performance.now() - started),
    });
    reportStage("connecting_kernel");
    await this.manager.connectToAnyKernel();
    if (version !== this.renderVersion) return;
    reportStage("waiting_for_pret");
    await this.manager.waitUntilReady();
    if (version !== this.renderVersion) return;
    reportStage("fetching_view");
    const viewData = await this.manager.resolveViewData(viewReference);
    // A closed or replaced view must not mount after its request finishes
    if (version !== this.renderVersion) return;
    reportStage("decoding_view");
    const makeView = this.manager.unpackView(viewData);
    reportStage("rendering_view");
    ReactDOM.render(
      <Suspense fallback={<div className="pret-host-loading">Loading…</div>}>
        {makeView()}
      </Suspense>,
      this.node
    );
    reportStage("render_returned");
  }

  dispose(): void {
    this.renderVersion++;
    ReactDOM.unmountComponentAtNode(this.node);
    this.release?.();
    this.release = null;
  }
}

export class PretService implements IPretService {
  constructor(private serviceManager: ServiceManager.IManager) {}

  createHost(sessionContext: ISessionContext, node: HTMLElement): IPretHost {
    const manager = getPretManager(sessionContext, this.serviceManager);
    return new PretHost(manager, node);
  }
}
