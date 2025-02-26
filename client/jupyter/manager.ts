import "regenerator-runtime/runtime";
import React from "react";

// @ts-ignore
import {JSONObject, JSONValue, UUID} from "@lumino/coreutils";
// @ts-ignore
import {DocumentRegistry} from "@jupyterlab/docregistry";
// @ts-ignore
import {IComm, IKernelConnection} from "@jupyterlab/services/lib/kernel/kernel";
// @ts-ignore
import {IChangedArgs} from "@jupyterlab/coreutils";
// @ts-ignore
import {Kernel} from "@jupyterlab/services";
// @ts-ignore
import {ISessionContext} from "@jupyterlab/apputils/lib/sessioncontext";
// @ts-ignore
import * as KernelMessage from "@jupyterlab/services/lib/kernel/messages";

import {loadPyodide, PyodideInterface} from "pyodide";
// @ts-ignore
import {PyProxy} from "pyodide/ffi";

import useSyncExternalStoreExports from 'use-sync-external-store/shim'

import {PretViewData} from "./widget";

React.useSyncExternalStore = useSyncExternalStoreExports.useSyncExternalStore;

import DESERIALIZE_PY from "../deserialize.py";

(window as any).React = React;

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
    private pyodide: PyodideInterface;
    private unpack: (data: string, unpickler_id: string, chunk_idx: number) => [PyProxy, PyProxy];
    private pyManager: PyProxy;
    private isStartingPython: boolean;
    public ready: Promise<any>;
    private _readyResolve: (value?: any) => void;
    private _readyReject: (reason?: any) => void;

    constructor(context: DocumentRegistry.IContext<DocumentRegistry.IModel>, settings: { saveState: boolean }) {

        this.commTargetName = 'pret';
        this.context = context;
        this.comm = null;
        this.pyodide = null;
        this.unpack = null;
        this.pyManager = null;
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

    startPython = () => {
        if (this.unpack) {
            return;
        }
        if (this.isStartingPython) {
            return;
        }
        this.isStartingPython = true;
        loadPyodide({indexURL: "https://cdn.jsdelivr.net/pyodide/v0.26.2/full/"}).then(async (pyodide) => {
            this.pyodide = pyodide;
            await pyodide.loadPackage("micropip");
            const micropip = pyodide.pyimport("micropip");
            await micropip.install("dill");
            this.unpack = await pyodide.runPythonAsync(DESERIALIZE_PY);
            this.isStartingPython = false;
        }).then(
            () => {
                // We don't need to wait for the comm to load the widgets
                //if (this.comm) {
                this._readyResolve();
                //}
            }
        ).catch((e) => {
            console.error(e);
            this._readyReject(e);
        });
    }

    sendMessage = (method: string, data: any) => {
        this.comm.send({
            'method': method,
            'data': data
        });
    }

    handleCommOpen = (comm: IComm, msg?: KernelMessage.ICommOpenMsg) => {
        console.info("Comm is open", comm.commId)
        this.comm = comm;
        this.comm.onMsg = this.handleCommMessage;
        if (this.unpack) {
            this._readyResolve();
        }
    };

    /**
     * Create a comm.
     */

    createComm = async (
        target_name: string,
        model_id: string,
        data?: JSONValue,
        metadata?: JSONObject,
        buffers?: (ArrayBuffer | ArrayBufferView)[]
    ): Promise<IComm> => {
        let kernel = this.context?.sessionContext.session?.kernel;
        if (!kernel) {
            throw new Error('No current kernel');
        }
        let comm = kernel.createComm(target_name, model_id);
        if (data || metadata) {
            comm.open(data, metadata, buffers);
        }
        return comm;
    }

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
        if (relevantCommIds.length > 0) {
            const comm = await this.createComm(
                this.commTargetName,
                relevantCommIds[0]);
            this.handleCommOpen(comm);
        }
    };


    handleCommMessage = (msg: KernelMessage.ICommMsgMsg) => {
        try {
            const {method, data} = msg.content.data as { method: string, data: any };
            this.pyManager.handle_message(method, this.pyodide.toPy(data));
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
     * Deserialize a view data to turn it into a callable python function, via pyodide.
     * @param view_data
     */
    unpackView({serialized, unpickler_id, chunk_idx}: PretViewData): any {
        const [renderable, manager] = this.unpack(serialized, unpickler_id, chunk_idx)
        this.pyManager = manager;
        this.pyManager.register_environment_handler(this);
        return renderable;
    }
}
