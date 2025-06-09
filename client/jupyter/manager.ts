import "regenerator-runtime/runtime";
import React from "react";

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

import useSyncExternalStoreExports from 'use-sync-external-store/shim'

import {PretViewData} from "./widget";

// @ts-ignore
React.useSyncExternalStore = useSyncExternalStoreExports.useSyncExternalStore;

import { makeLoadApp } from "../appLoader";

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
    private unpack: (data: any, unpickler_id: string, chunk_idx: number) => [any, any];
    private appManager: any;
    public ready: Promise<any>;
    private _readyResolve: (value?: any) => void;
    private _readyReject: (reason?: any) => void;

    constructor(context: DocumentRegistry.IContext<DocumentRegistry.IModel>, settings: { saveState: boolean }) {

        this.commTargetName = 'pret';
        this.context = context;
        this.comm = null;
        this.unpack = makeLoadApp();
        this.appManager = null;
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
        try {
            const {method, data} = msg.content.data as { method: string, data: any };
            this.appManager.handle_message(method, data);
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
        const [renderable, manager] = this.unpack(serialized, marshaler_id, chunk_idx)
        this.appManager = manager;
        this.appManager.register_environment_handler(this);
        return renderable;
    }
}
