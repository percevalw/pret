import "regenerator-runtime/runtime";
import React from "react";

// @ts-ignore
import { DocumentRegistry } from "@jupyterlab/docregistry";
// @ts-ignore
import { IComm, IKernelConnection } from "@jupyterlab/services/lib/kernel/kernel";
// @ts-ignore
import { IChangedArgs } from "@jupyterlab/coreutils";
// @ts-ignore
import { Kernel } from "@jupyterlab/services";
// @ts-ignore
import { ISessionContext } from "@jupyterlab/apputils/lib/sessioncontext";
// @ts-ignore
import * as KernelMessage from "@jupyterlab/services/lib/kernel/messages";

import useSyncExternalStoreExports from "use-sync-external-store/shim";

import { PretSerialized, PretViewData } from "./widget";
import { makeLoadApp } from "../appLoader";

type BundleResponse = {
    serialized: PretSerialized;
    maxChunkIdx: number;
};
const PRET_BUNDLE_REQUEST_TIMEOUT_MS = 10000;

// @ts-ignore
React.useSyncExternalStore = useSyncExternalStoreExports.useSyncExternalStore;

export default class PretJupyterHandler {
    get readyResolve(): any {
        return this._readyResolve;
    }

    set readyResolve(value: any) {
        this._readyResolve = value;
    }

    private context: DocumentRegistry.IContext<DocumentRegistry.IModel>;
    private isDisposed: boolean;
    private readonly commTargetName: string;
    private settings: { saveState: boolean };

    private comm: IComm;

    // Lock promise to chain events, and avoid concurrent state access
    // Each event calls .then on this promise and replaces it to queue itself
    private unpack: ReturnType<typeof makeLoadApp>;
    private appManager: any;
    private bundleRequests: Map<
        string,
        { resolve: (value: BundleResponse) => void; reject: (reason?: any) => void }
    >;
    private bundleCache: Map<string, BundleResponse>;
    private bundleInFlight: Map<string, Promise<BundleResponse>>;
    private bundleRequestSeq: number;
    public ready: Promise<any>;
    private _readyResolve: (value?: any) => void;
    private _readyReject: (reason?: any) => void;

    constructor(context: DocumentRegistry.IContext<DocumentRegistry.IModel>, settings: { saveState: boolean }) {

        this.commTargetName = 'pret';
        this.context = context;
        this.comm = null;
        this.unpack = makeLoadApp();
        this.appManager = null;
        this.bundleRequests = new Map();
        this.bundleCache = new Map();
        this.bundleInFlight = new Map();
        this.bundleRequestSeq = 0;
        this.ready = new Promise((resolve, reject) => {
            this._readyResolve = resolve;
            this._readyReject = reject;
        });

        // https://github.com/jupyter-widgets/ipywidgets/commit/5b922f23e54f3906ed9578747474176396203238
        context?.sessionContext.kernelChanged.connect((
            sender: ISessionContext,
            args: IChangedArgs<Kernel.IKernelConnection | null, Kernel.IKernelConnection | null, 'kernel'>
        ) => {
            this.handleKernelChanged(args);
        });

        context?.sessionContext.statusChanged.connect((
            sender: ISessionContext,
            status: Kernel.Status,
        ) => {
            this.handleKernelStatusChange(status);
        });

        if (context?.sessionContext.session?.kernel) {
            this.handleKernelChanged({
                name: 'kernel',
                oldValue: null,
                newValue: context.sessionContext.session?.kernel
            });
        }

        this.connectToAnyKernel().then();

        this.settings = settings;
    }

    sendMessage = async (method: string, data: any) => {
        if (!this.comm) {
            const error: any = new Error("Jupyter communication channel is not available.");
            error.code = "PRET_JUPYTER_DOWN";
            throw error;
        }
        try {
            return this.comm.send({
              method: method,
              data: data,
            });
        } catch (e: any) {
            const message = String(e?.message ?? e);
            const error: any = new Error(message);
            const disconnected =
                this.context?.sessionContext?.session?.kernel?.connectionStatus &&
                this.context.sessionContext.session.kernel.connectionStatus !== "connected";
            error.code = disconnected
                ? "PRET_JUPYTER_DOWN"
                : message.includes("Kernel")
                  ? "PRET_KERNEL_DOWN"
                  : "PRET_JUPYTER_DOWN";
            throw error;
        }
    }

    handleCommOpen = (comm: IComm, msg?: KernelMessage.ICommOpenMsg) => {
        console.info("Comm is open", comm.commId)
        this.comm = comm;
        this.comm.onMsg = this.handleCommMessage;
        this._readyResolve();
    };

    /**
     * Get the currently-registered comms.
     */
    getCommInfo = async (): Promise<any> => {
        let kernel = this.context?.sessionContext.session?.kernel;
        if (!kernel) {
            throw new Error('No current kernel');
        }
        const reply = await kernel.requestCommInfo({target_name: this.commTargetName});
        if (reply.content.status === 'ok') {
            return (reply.content).comms;
        } else {
            return {};
        }
    }

    connectToAnyKernel = async () => {
        if (!this.context?.sessionContext) {
            console.warn("No session context")
            return;
        }
        console.info("Awaiting session to be ready")
        await this.context.sessionContext.ready;

        if (this.context?.sessionContext.session.kernel.handleComms === false) {
            console.warn("Comms are disabled")
            return;
        }
        const allCommIds = await this.getCommInfo();
        const relevantCommIds = Object.keys(allCommIds).filter(key => allCommIds[key]['target_name'] === this.commTargetName);
        console.info("Jupyter annotator comm ids", relevantCommIds, "(there should be at most one)");
        if (relevantCommIds.length === 0) {
            const comm = this.context?.sessionContext.session?.kernel.createComm(this.commTargetName);
            comm.open()
            this.handleCommOpen(comm);
        }
        else if (relevantCommIds.length >= 1) {
            if (relevantCommIds.length > 1) {
                console.warn("Multiple comms found for target name", this.commTargetName, "using the first one");
            }
            const comm = this.context?.sessionContext.session?.kernel.createComm(this.commTargetName, relevantCommIds[0]);
            // comm.open()
            this.handleCommOpen(comm);
        }
    };


    handleCommMessage = (msg: KernelMessage.ICommMsgMsg) => {
        const msgContent = msg?.content?.data as
            | { method?: string; data?: Record<string, unknown> }
            | undefined;
        if (msgContent?.method === "bundle_response") {
            const payload = (msgContent.data ?? {}) as {
                request_id?: string;
                marshaler_id?: string;
                serialized?: PretSerialized;
                error?: string;
                max_chunk_idx?: number;
            };
            const {request_id, marshaler_id, serialized, error} = payload;
            const maxChunkIdx =
                typeof payload.max_chunk_idx === "number" ? payload.max_chunk_idx : -1;
            const pending = request_id ? this.bundleRequests.get(request_id) : null;
            if (pending) {
                this.bundleRequests.delete(request_id);
                if (error) {
                    const typedError: any = new Error(error);
                    typedError.code = error.includes("not found in current session")
                        ? "PRET_STALE_MARSHALER"
                        : "PRET_BUNDLE_ERROR";
                    pending.reject(typedError);
                } else if (!serialized) {
                    pending.reject(
                        new Error(
                            `Bundle response for ${marshaler_id || "unknown"} was empty`
                        )
                    );
                } else if (maxChunkIdx < 0) {
                    pending.reject(
                        new Error(
                            `Bundle response for ${marshaler_id || "unknown"} did not include max_chunk_idx`
                        )
                    );
                } else {
                    pending.resolve({
                        serialized: serialized as PretSerialized,
                        maxChunkIdx,
                    });
                }
            } else {
                console.warn("No pending bundle request found", request_id, marshaler_id);
            }
            return;
        }
        try {
            this.appManager.handle_comm_message(msg);
        } catch (e) {
            console.error("Error during comm message reception", e);
        }
    };

    /**
     * Register a new kernel
     */
    handleKernelChanged = (
        {
            name,
            oldValue,
            newValue
        }: { name: string, oldValue: IKernelConnection | null, newValue: IKernelConnection | null }) => {
        console.info("handleKernelChanged", oldValue, newValue);
        if (oldValue) {
            this.comm = null;
            oldValue.removeCommTarget(this.commTargetName, this.handleCommOpen);
        }

        if (newValue) {
            newValue.registerCommTarget(this.commTargetName, this.handleCommOpen);
        }
    };

    handleKernelStatusChange = (status: Kernel.Status) => {
        switch (status) {
            case 'autorestarting':
            case 'restarting':
            case 'dead':
                //this.disconnect();
                break;
            default:
        }
    };

    /**
     * Deserialize a view data to turn it into a callable js function
     * @param view_data
     */
    unpackView({serialized, marshaler_id, chunk_idx}: PretViewData): any {
        if (!serialized) {
            throw new Error(`Missing serialized bundle for marshaler ${marshaler_id}`);
        }
        const [renderable, manager] = this.unpack(serialized, marshaler_id, chunk_idx)
        this.appManager = manager;
        this.appManager.register_environment_handler(this);
        return renderable;
    }

    async fetchBundle(marshalerId: string, minChunkIdx?: number): Promise<PretSerialized> {
        const cached = this.bundleCache.get(marshalerId);
        if (cached && (minChunkIdx === undefined || minChunkIdx <= cached.maxChunkIdx)) {
            return cached.serialized;
        }
        const inflightCached = this.bundleInFlight.get(marshalerId);
        if (inflightCached) {
            const inflightResult = await inflightCached;
            if (
                minChunkIdx === undefined ||
                minChunkIdx <= inflightResult.maxChunkIdx
            ) {
                return inflightResult.serialized;
            }
        }

        const requestId = `bundle-${++this.bundleRequestSeq}`;
        let resolveFn!: (value: BundleResponse) => void;
        let rejectFn!: (reason?: any) => void;
        const pending = new Promise<BundleResponse>((resolve, reject) => {
            resolveFn = resolve;
            rejectFn = reject;
        });
        this.bundleRequests.set(requestId, {resolve: resolveFn, reject: rejectFn});
        const timeout = setTimeout(() => {
            const pendingRequest = this.bundleRequests.get(requestId);
            if (!pendingRequest) {
                return;
            }
            this.bundleRequests.delete(requestId);
            const error: any = new Error(
                `Timed out while waiting for PRET bundle response for ${marshalerId}.`
            );
            const disconnected =
                this.context?.sessionContext?.session?.kernel?.connectionStatus &&
                this.context.sessionContext.session.kernel.connectionStatus !== "connected";
            error.code = disconnected ? "PRET_JUPYTER_DOWN" : "PRET_BUNDLE_TIMEOUT";
            pendingRequest.reject(error);
        }, PRET_BUNDLE_REQUEST_TIMEOUT_MS);

        const inflight = pending
            .then((bundle) => {
                this.bundleCache.set(marshalerId, bundle);
                return bundle;
            })
            .finally(() => {
                clearTimeout(timeout);
                this.bundleInFlight.delete(marshalerId);
            });
        this.bundleInFlight.set(marshalerId, inflight);

        await this.ready;
        if (!this.context?.sessionContext.session?.kernel) {
            this.bundleRequests.delete(requestId);
            this.bundleInFlight.delete(marshalerId);
            clearTimeout(timeout);
            const error: any = new Error("No active kernel is available for this notebook.");
            error.code = "PRET_KERNEL_DOWN";
            throw error;
        }
        try {
            this.sendMessage("bundle_request", {
                marshaler_id: marshalerId,
                request_id: requestId,
            });
        } catch (err) {
            this.bundleRequests.delete(requestId);
            this.bundleInFlight.delete(marshalerId);
            throw err;
        }

        const bundle = await inflight;
        if (minChunkIdx !== undefined && minChunkIdx > bundle.maxChunkIdx) {
            throw new Error(
                `Bundle for ${marshalerId} only includes chunk ${bundle.maxChunkIdx}, requested ${minChunkIdx}`
            );
        }
        return bundle.serialized;
    }

    async resolveViewData(viewData: PretViewData): Promise<PretViewData> {
        if (viewData.serialized) {
            return viewData;
        }
        const serialized = await this.fetchBundle(
            viewData.marshaler_id,
            viewData.chunk_idx
        );
        return {
            ...viewData,
            serialized,
        };
    }

    clearBundleCache(marshalerId: string) {
        this.bundleCache.delete(marshalerId);
    }

    clearUnpackCache(marshalerId: string) {
        this.unpack.clearCache(marshalerId);
    }
}
