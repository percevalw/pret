import "regenerator-runtime/runtime";
import React from "react";

// @ts-ignore
// @ts-ignore
import { IComm, IKernelConnection } from "@jupyterlab/services/lib/kernel/kernel";
// @ts-ignore
import { IChangedArgs } from "@jupyterlab/coreutils";
// @ts-ignore
import { ISessionContext } from "@jupyterlab/apputils/lib/sessioncontext";
// @ts-ignore
import { ServiceManager } from "@jupyterlab/services";
// @ts-ignore
import * as KernelMessage from "@jupyterlab/services/lib/kernel/messages";

import useSyncExternalStoreExports from "use-sync-external-store/shim";

import { PretViewData } from "./widget";
import { makeLoadApp } from "../appLoader";
import type { PretSerialized } from "../appLoader";

type BundleResponse = {
    serialized: PretSerialized;
    maxChunkIdx: number;
    byteLength: number;
};

type BundleRequest = {
    resolve: (value: BundleResponse) => void;
    reject: (reason?: any) => void;
    marshalerId: string;
    byteOffset: number;
};

type PretConnectionState = {
    connected: boolean | null;
    reason: string | null;
    transport: string | null;
    kernelConnectionStatus: string | null;
    lastError: string | null;
};

const PRET_BUNDLE_REQUEST_TIMEOUT_MS = 15000;
const PRET_RECOVERY_POLL_INTERVAL_MS = 3000;
const PRET_RECOVERY_REQUEST_TIMEOUT_MS = 5000;
const PRET_ALIVE_REQUEST_TIMEOUT_MS = 5000;
const PRET_PROTOCOL_VERSION = 2;

type AliveRequest = {
  resolve: () => void;
  reject: (reason?: any) => void;
  timeout: number;
};

type ReadyWaiter = {
  resolve: () => void;
  reject: (reason?: any) => void;
};

type ReadyState =
  | { status: "pending" }
  | { status: "connected" }
  | { status: "failed"; error: any };

const toUint8Array = (
    buffer?: ArrayBuffer | ArrayBufferView
): Uint8Array<ArrayBuffer> => {
    if (!buffer) {
        return new Uint8Array(0);
    }
    if (buffer instanceof ArrayBuffer) {
        return new Uint8Array(buffer);
    }
    return new Uint8Array(
        buffer.buffer as ArrayBuffer,
        buffer.byteOffset,
        buffer.byteLength
    );
};

const concatUint8Arrays = (left: Uint8Array, right: Uint8Array): Uint8Array => {
    const merged = new Uint8Array(left.length + right.length);
    merged.set(left);
    merged.set(right, left.length);
    return merged;
};

const maybeDecompressBytes = async (
    bytes: Uint8Array<ArrayBuffer>,
    compression?: string
): Promise<Uint8Array> => {
    if (!compression) {
        return bytes;
    }
    if (compression !== "gzip") {
        throw new Error(`Unsupported PRET bundle compression: ${compression}`);
    }
    if (typeof DecompressionStream !== "function") {
        throw new Error("DecompressionStream is not available in this browser.");
    }
    const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
    return new Uint8Array(await new Response(stream).arrayBuffer());
};

// @ts-ignore
React.useSyncExternalStore = useSyncExternalStoreExports.useSyncExternalStore;

export default class PretJupyterHandler {
  private context: {sessionContext: ISessionContext; path?: string; save?: () => Promise<void>};
  private readonly commTargetName: string;

  private comm: IComm;

  private unpack: ReturnType<typeof makeLoadApp>;
  private appManager: any;
  private bundleRequests: Map<string, BundleRequest>;
  private bundleCache: Map<string, BundleResponse>;
  private bundleInFlight: Map<string, Promise<BundleResponse>>;
  private bundleRequestSeq: number;
  private recoveryPollTimer: number | null;
  private recoveryPollInFlight: boolean;
  private aliveRequestSeq: number;
  private aliveRequests: Map<string, AliveRequest>;
  private readyState: ReadyState;
  private readyWaiters: Set<ReadyWaiter>;
  private readyListeners: Set<() => void>;
  private connectionState: PretConnectionState;
  private pendingBackendOpenComm: IComm | null;
  private consumers = new Set<symbol>();
  private disposed = false;
  private generation = 0;
  private connecting: Promise<void> | null = null;
  private kernel: IKernelConnection | null = null;
  private kernelChanged = (
    _sender: ISessionContext,
    args: IChangedArgs<IKernelConnection | null, IKernelConnection | null, "kernel">
  ) => this.handleKernelChanged(args);
  private connectionFailed = (_sender: ServiceManager.IManager, error: Error) => this.handleConnectionFailure(error);

  constructor(
    context: {sessionContext: ISessionContext; path?: string; save?: () => Promise<void>},
    private serviceManager: ServiceManager.IManager
  ) {
    this.commTargetName = "pret";
    this.context = context;
    this.comm = null;
    this.unpack = makeLoadApp();
    this.appManager = null;
    this.bundleRequests = new Map();
    this.bundleCache = new Map();
    this.bundleInFlight = new Map();
    this.bundleRequestSeq = 0;
    this.recoveryPollTimer = null;
    this.recoveryPollInFlight = false;
    this.aliveRequestSeq = 0;
    this.aliveRequests = new Map();
    this.readyState = { status: "pending" };
    this.readyWaiters = new Set();
    this.readyListeners = new Set();
    this.pendingBackendOpenComm = null;
    this.connectionState = {
      connected: false,
      reason: "initializing",
      transport: "jupyter-comm",
      kernelConnectionStatus: null,
      lastError: null,
    };

    context.sessionContext.kernelChanged.connect(this.kernelChanged);
    serviceManager.connectionFailure.connect(this.connectionFailed);

    if (context?.sessionContext.session?.kernel) {
      this.handleKernelChanged({
        name: "kernel",
        oldValue: null,
        newValue: context.sessionContext.session?.kernel,
      });
    }

    this.propagateConnectionState({
      kernelConnectionStatus: this.getKernelConnectionStatus(),
    });
  }

  // Views share one session manager, only live consumers enable connection attempts
  acquire = (): (() => void) => {
    if (this.disposed) throw new Error("PRET manager disposed.");
    const consumer = Symbol();
    this.consumers.add(consumer);
    if (this.consumers.size === 1) void this.connectToAnyKernel();
    return () => {
      if (this.consumers.delete(consumer) && this.consumers.size === 0) {
        this.stopConnection("PRET has no active views.", false);
      }
    };
  };

  // Stop view loading and recovery, an idle session still receives replies to edits already sent
  private stopConnection = (reason: string, detach = true) => {
    this.generation++;
    this.connecting = null;
    this.stopRecoveryPolling();
    this.rejectAliveRequests(reason);
    const error: any = new Error(reason);
    error.code = "PRET_JUPYTER_DOWN";
    this.failReady(error);
    for (const pending of this.bundleRequests.values()) pending.reject(error);
    this.bundleRequests.clear();
    this.bundleInFlight.clear();
    if (detach && this.comm) {
      this.comm.onMsg = null;
      this.comm.onClose = null;
      this.comm = null;
    }
    this.pendingBackendOpenComm = null;
    this.propagateConnectionState({ connected: false, reason: "inactive" });
  };

  dispose = () => {
    if (this.disposed) return;
    this.disposed = true;
    this.consumers.clear();
    this.context.sessionContext.kernelChanged.disconnect(this.kernelChanged);
    this.serviceManager.connectionFailure.disconnect(this.connectionFailed);
    this.kernel?.removeCommTarget(this.commTargetName, this.handleCommOpen);
    this.stopConnection("PRET manager disposed.");
    this.readyListeners.clear();
  };

  private getKernelConnectionStatus = (): string | null => {
    return (
      this.context?.sessionContext?.session?.kernel?.connectionStatus ?? null
    );
  };

  waitUntilReady = (): Promise<void> => {
    if (this.readyState.status === "connected") {
      return Promise.resolve();
    }
    if (this.readyState.status === "failed") {
      return Promise.reject(this.readyState.error);
    }
    return new Promise((resolve, reject) => {
      this.readyWaiters.add({ resolve, reject });
    });
  };

  subscribeReadyChange = (listener: () => void): (() => void) => {
    this.readyListeners.add(listener);
    return () => {
      this.readyListeners.delete(listener);
    };
  };

  private failReady = (error: any) => {
    this.readyState = { status: "failed", error };
    for (const waiter of this.readyWaiters) {
      waiter.reject(error);
    }
    this.readyWaiters.clear();
  };

  private makeKernelDownError = (): any => {
    const msg = `It seems the app is not running, please restart the notebook.`;
    const err: any = new Error(msg);
    err.code = "PRET_KERNEL_DOWN";
    return err;
  };

  private propagateConnectionState = (patch: Partial<PretConnectionState>) => {
    this.connectionState = {
      ...this.connectionState,
      ...patch,
    };
    const manager = this.appManager as any;
    if (!manager || typeof manager.set_connection_status !== "function") {
      return;
    }
    try {
      manager.set_connection_status(
        this.connectionState.connected,
        this.connectionState.reason,
        this.connectionState.transport,
        this.connectionState.kernelConnectionStatus,
        this.connectionState.lastError
      );
    } catch (error) {
      console.warn("PRET: Failed to propagate PRET connection state", error);
    }
  };

  private withTimeout = async <T>(
    operation: Promise<T>,
    timeoutMs: number,
    message: string
  ): Promise<T> => {
    let timeout: number | null = null;
    try {
      return await new Promise<T>((resolve, reject) => {
        timeout = window.setTimeout(() => {
          reject(new Error(message));
        }, timeoutMs);
        operation.then(resolve, reject);
      });
    } finally {
      if (timeout !== null) {
        window.clearTimeout(timeout);
      }
    }
  };

  private markConnected = (reason: string) => {
    if (this.disposed || this.consumers.size === 0) return;
    const wasConnected = this.connectionState.connected === true;
    this.pendingBackendOpenComm = null;
    this.stopRecoveryPolling();
    this.propagateConnectionState({
      connected: true,
      reason,
      transport: "jupyter-comm",
      kernelConnectionStatus: this.getKernelConnectionStatus(),
      lastError: null,
    });
    this.readyState = { status: "connected" };
    for (const waiter of this.readyWaiters) {
      waiter.resolve();
    }
    this.readyWaiters.clear();
    if (!wasConnected) {
      for (const listener of Array.from(this.readyListeners)) {
        listener();
      }
    }
  };

  private markDisconnected = (patch: Partial<PretConnectionState>) => {
    if (this.disposed || this.consumers.size === 0) return;
    if (this.readyState.status === "connected") {
      this.readyState = { status: "pending" };
    }
    this.propagateConnectionState({
      connected: false,
      transport: "jupyter-comm",
      kernelConnectionStatus: this.getKernelConnectionStatus(),
      ...patch,
    });
    this.startRecoveryPolling();
  };

  private startRecoveryPolling = (delayMs = 0) => {
    if (
      this.disposed || this.consumers.size === 0 || this.readyState.status === "failed" ||
      this.connectionState.connected === true ||
      this.recoveryPollInFlight ||
      this.recoveryPollTimer !== null
    ) {
      return;
    }
    this.recoveryPollTimer = window.setTimeout(() => {
      this.recoveryPollTimer = null;
      void this.pollForRecovery();
    }, delayMs);
  };

  private stopRecoveryPolling = () => {
    if (this.recoveryPollTimer !== null) {
      window.clearTimeout(this.recoveryPollTimer);
      this.recoveryPollTimer = null;
    }
  };

  private pollForRecovery = async () => {
    if (
      this.disposed || this.consumers.size === 0 || this.readyState.status === "failed" ||
      this.connectionState.connected === true ||
      this.recoveryPollInFlight
    ) {
      return;
    }
    this.recoveryPollInFlight = true;
    try {
      await this.connectToAnyKernel();
    } catch (error) {
      console.info("PRET: Recovery probe failed", error);
    } finally {
      this.recoveryPollInFlight = false;
      if ((this.connectionState.connected as boolean | null) !== true) {
        this.startRecoveryPolling(PRET_RECOVERY_POLL_INTERVAL_MS);
      }
    }
  };

  private sendAliveRequest = (trigger: string): Promise<void> => {
    if (!this.comm) {
      return Promise.reject(
        new Error("Cannot send PRET liveness request without a comm.")
      );
    }

    const requestId = `alive-${++this.aliveRequestSeq}`;
    return new Promise<void>((resolve, reject) => {
      const timeout = window.setTimeout(() => {
        this.aliveRequests.delete(requestId);
        reject(
          new Error(
            `Timed out while waiting for PRET liveness response for ${requestId}.`
          )
        );
      }, PRET_ALIVE_REQUEST_TIMEOUT_MS);
      this.aliveRequests.set(requestId, { resolve, reject, timeout });

      try {
        this.comm.send({
          method: "is_alive_request",
          data: {
            request_id: requestId,
            trigger,
            protocol_version: PRET_PROTOCOL_VERSION,
          },
        });
      } catch (error) {
        window.clearTimeout(timeout);
        this.aliveRequests.delete(requestId);
        reject(error);
      }
    });
  };

  private handleAliveResponse = (data?: Record<string, unknown>) => {
    const requestId =
      typeof data?.request_id === "string" ? data.request_id : null;
    const pending = requestId ? this.aliveRequests.get(requestId) : null;
    if (!pending) {
      this.markBackendMessageReceived("is_alive_response");
      return;
    }
    this.aliveRequests.delete(requestId);
    window.clearTimeout(pending.timeout);
    if (
      data?.ok !== true ||
      data?.protocol_version !== PRET_PROTOCOL_VERSION
    ) {
      pending.reject(
        new Error(
          String(
            data?.error ??
              `PRET protocol ${String(data?.protocol_version)} is incompatible with frontend protocol ${PRET_PROTOCOL_VERSION}`
          )
        )
      );
    } else {
      pending.resolve();
    }
  };

  private rejectAliveRequests = (reason: string) => {
    for (const [requestId, pending] of Array.from(this.aliveRequests)) {
      this.aliveRequests.delete(requestId);
      window.clearTimeout(pending.timeout);
      pending.reject(new Error(reason));
    }
  };

  sendMessage = async (method: string, data: any) => {
    if (!this.comm) {
      const error: any = new Error(
        "Jupyter communication channel is not available."
      );
      error.code = "PRET_JUPYTER_DOWN";
      this.markDisconnected({
        reason: "comm_missing",
        lastError: String(error.message ?? error),
      });
      throw error;
    }
    try {
      return this.comm.send({
        method: method,
        data: data,
      });
    } catch (e: any) {
      const message = String(e?.message ?? e);
      const error: any = e instanceof Error ? e : new Error(message);
      const disconnected =
        this.context?.sessionContext?.session?.kernel?.connectionStatus &&
        this.context.sessionContext.session.kernel.connectionStatus !==
          "connected";
      if (!error.code) {
        error.code = disconnected
          ? "PRET_JUPYTER_DOWN"
          : message.includes("Kernel")
          ? "PRET_KERNEL_DOWN"
          : "PRET_JUPYTER_DOWN";
      }
      this.markDisconnected({
        reason: String(error.code ?? "send_failed"),
        lastError: message,
      });
      throw error;
    }
  };

  private markBackendMessageReceived = (method?: string) => {
    this.markConnected(method ? `message_${method}` : "message_received");
  };

  private attachComm = (
    comm: IComm,
    options: { failIfClosedBeforeBackendOpen?: boolean } = {}
  ) => {
    if (options.failIfClosedBeforeBackendOpen) {
      this.pendingBackendOpenComm = comm;
    }
    this.comm = comm;
    this.comm.onMsg = (msg) => {
      if (this.comm === comm && !this.disposed) return this.handleCommMessage(msg);
    };
    console.info("PRET: Attached comm", {
      target: this.commTargetName,
      commId: comm.commId,
      kernelId: this.context?.sessionContext.session?.kernel?.id ?? null,
      waitingForBackendOpen: options.failIfClosedBeforeBackendOpen === true,
    });
    this.comm.onClose = () => {
      if (this.comm !== comm || this.disposed) return;
      const closedBeforeBackendOpen = this.pendingBackendOpenComm === comm;
      if (closedBeforeBackendOpen) {
        this.pendingBackendOpenComm = null;
        this.comm = null;
        this.rejectAliveRequests("PRET comm closed.");
        console.error(
          "PRET: Created comm closed before backend comm opened",
          {
            target: this.commTargetName,
            commId: comm.commId,
            kernelId: this.context?.sessionContext.session?.kernel?.id ?? null,
            kernelStatus:
              this.context?.sessionContext.session?.kernel?.connectionStatus ?? null,
          }
        );
        const err = this.makeKernelDownError();
        this.failReady(err);
        this.markDisconnected({
          reason: "comm_closed_before_backend_open",
          lastError: err.message,
        });
        return;
      }
      this.comm = null;
      this.rejectAliveRequests("PRET comm closed.");
      this.markDisconnected({
        reason: "comm_closed",
      });
    };
  };

  private confirmCommAlive = async (trigger: string): Promise<boolean> => {
    const generation = this.generation;
    const comm = this.comm;
    try {
      await this.sendAliveRequest(trigger);
      if (generation !== this.generation || this.comm !== comm) return false;
      this.markConnected("pret_alive");
      return true;
    } catch (error) {
      if (generation !== this.generation || this.comm !== comm) return false;
      this.comm = null;
      this.markDisconnected({
        reason: "pret_alive_timeout",
        lastError: String((error as any)?.message ?? error),
      });
      return false;
    }
  };

  handleCommOpen = (comm: IComm, msg?: KernelMessage.ICommOpenMsg) => {
    if (this.disposed || this.consumers.size === 0) return;
    this.readyState = { status: "pending" };
    console.info("PRET: Backend comm opened", {
      target: this.commTargetName,
      commId: comm.commId,
      kernelId: this.context?.sessionContext.session?.kernel?.id ?? null,
      messageType: msg?.header?.msg_type ?? null,
    });
    this.pendingBackendOpenComm = null;
    this.attachComm(comm);
    void this.confirmCommAlive("comm_open");
  };

  /**
   * Get the currently-registered comms.
   */
  getCommInfo = async (): Promise<any> => {
    let kernel = this.context?.sessionContext.session?.kernel;
    if (!kernel) {
      throw new Error("No current kernel");
    }
    console.info("PRET: Requesting comm info", {
      target: this.commTargetName,
      kernelId: kernel.id,
      kernelStatus: kernel.connectionStatus,
    });
    const reply = await kernel.requestCommInfo({
      target_name: this.commTargetName,
    });
    if (reply.content.status === "ok") {
      console.info("PRET: Comm info received", {
        target: this.commTargetName,
        kernelId: kernel.id,
        commIds: Object.keys(reply.content.comms),
      });
      return reply.content.comms;
    } else {
      return {};
    }
  };

  private attachCommFromInfo = (allCommIds: any): boolean => {
    const kernel = this.context?.sessionContext.session?.kernel;
    if (!kernel) {
      return false;
    }
    const relevantCommIds = Object.keys(allCommIds).filter(
      (key) => allCommIds[key]["target_name"] === this.commTargetName
    );
    console.info(
      "PRET: Jupyter annotator comm ids",
      relevantCommIds,
      "(there should be at most one)"
    );
    if (relevantCommIds.length > 1) {
      console.warn(
        "PRET: Multiple comms found for target name",
        this.commTargetName,
        "using the last one"
      );
    }

    const commId = relevantCommIds.length ? relevantCommIds.pop() : null;
    const createdCommWaitingForBackendOpen = commId === null;
    const comm = commId && kernel.hasComm(commId)
      ? // JupyterLab exposes hasComm/createComm, but not getComm.
        (kernel as any)._comms?.get(commId)
      : kernel.createComm(this.commTargetName, commId ?? undefined);
    if (!comm) {
      console.warn("PRET: Existing comm was not retrievable", commId);
      const err = this.makeKernelDownError();
      this.failReady(err);
      throw err;
    }
    this.attachComm(comm, {
      failIfClosedBeforeBackendOpen: createdCommWaitingForBackendOpen,
    });
    if (createdCommWaitingForBackendOpen) {
      console.info("PRET: No existing comm found; opening comm", comm.commId);
      try {
        comm.open({});
      } catch (error) {
        if (this.pendingBackendOpenComm === comm) {
          this.pendingBackendOpenComm = null;
        }
        this.comm = null;
        const err: any =
          error instanceof Error ? error : new Error(String(error));
        if (!err.code) {
          err.code = "PRET_KERNEL_DOWN";
        }
        this.failReady(err);
        throw err;
      }
    }
    return true;
  };

  // Concurrent hosts share one handshake, explicit openings may retry a rejected handshake
  connectToAnyKernel = (): Promise<void> => {
    if (this.disposed || this.consumers.size === 0) return Promise.resolve();
    if (this.connecting) return this.connecting;
    if (this.readyState.status === "failed") this.readyState = { status: "pending" };
    const generation = this.generation;
    const operation = this.connect(generation).catch((error) => {
      if (generation === this.generation) this.failReady(error);
    }).finally(() => {
      if (this.connecting === operation) this.connecting = null;
    });
    this.connecting = operation;
    return operation;
  };

  private connect = async (generation: number) => {
    if (this.comm) {
      if (!this.connectionState.connected) await this.confirmCommAlive("recovery");
      return;
    }
    if (!this.context?.sessionContext) {
      console.warn("PRET: No session context");
      this.markDisconnected({
        reason: "missing_session_context",
        kernelConnectionStatus: null,
      });
      return;
    }
    console.info("PRET: Awaiting session to be ready");
    await this.context.sessionContext.ready;
    if (generation !== this.generation || this.comm) {
      return;
    }

    if (this.context?.sessionContext.session?.kernel?.handleComms === false) {
      console.warn("PRET: Comms are disabled");
      this.markDisconnected({
        reason: "comms_disabled",
      });
      return;
    }
    let allCommIds;
    try {
      allCommIds = await this.withTimeout(
        this.getCommInfo(),
        PRET_RECOVERY_REQUEST_TIMEOUT_MS,
        "Timed out while checking PRET comm info."
      );
    } catch (error) {
      if (generation !== this.generation) return;
      this.markDisconnected({
        reason: "comm_info_failed",
        lastError: String((error as any)?.message ?? error),
      });
      return;
    }
    if (generation !== this.generation || this.comm) return;
    if (this.attachCommFromInfo(allCommIds)) await this.confirmCommAlive("connect_to_kernel");
  };

  handleCommMessage = async (msg: KernelMessage.ICommMsgMsg) => {
    const msgContent = msg?.content?.data as
      | { method?: string; data?: Record<string, unknown> }
      | undefined;
    if (msgContent?.method === "is_alive_response") {
      this.handleAliveResponse(msgContent.data);
      return;
    }
    if (msgContent?.method) {
      this.markBackendMessageReceived(msgContent.method);
    }
    if (msgContent?.method === "bundle_response") {
      const payload = (msgContent.data ?? {}) as {
        request_id?: string;
        marshaler_id?: string;
        code?: string;
        error?: string;
        max_chunk_idx?: number;
        byte_offset?: number;
        byte_length?: number;
        compression?: string | null;
      };
      const { request_id, marshaler_id, code, error } = payload;
      const maxChunkIdx =
        typeof payload.max_chunk_idx === "number" ? payload.max_chunk_idx : -1;
      const byteOffset =
        typeof payload.byte_offset === "number" ? payload.byte_offset : -1;
      const byteLength =
        typeof payload.byte_length === "number" ? payload.byte_length : -1;
      const compression =
        typeof payload.compression === "string"
          ? payload.compression
          : undefined;
      const pending = request_id ? this.bundleRequests.get(request_id) : null;
      if (pending) {
        this.bundleRequests.delete(request_id);
        if (error) {
          const typedError: any = new Error(error);
          typedError.code = error.includes("not found in current session")
            ? "PRET_STALE_MARSHALER"
            : "PRET_BUNDLE_ERROR";
          pending.reject(typedError);
        } else if (!code) {
          pending.reject(
            new Error(
              `Bundle response for ${marshaler_id || "unknown"} was empty`
            )
          );
        } else if (maxChunkIdx < 0 || byteOffset < 0 || byteLength < 0) {
          pending.reject(
            new Error(
              `Bundle response for ${marshaler_id || "unknown"} was incomplete`
            )
          );
        } else {
          const delta = await maybeDecompressBytes(
            toUint8Array(msg.buffers?.[0]),
            compression
          );
          let bytes = delta;
          if (byteOffset > 0) {
            const cached = this.bundleCache.get(pending.marshalerId);
            if (!cached || cached.byteLength !== pending.byteOffset) {
              pending.reject(
                new Error(
                  `Bundle cache for ${pending.marshalerId} is out of sync with byte offset ${byteOffset}`
                )
              );
              return;
            }
            bytes = concatUint8Arrays(
              cached.serialized[0] as Uint8Array,
              delta
            );
          }
          pending.resolve({
            serialized: [bytes, code],
            maxChunkIdx,
            byteLength,
          });
        }
      } else {
        console.warn(
          "PRET: No pending bundle request found",
          request_id,
          marshaler_id
        );
      }
      return;
    }
    if (msgContent?.method === "call_success") {
      const value = (msgContent.data as any)?.value;
      if (value?.__pret_binary__ === true) {
        const { __pret_binary__, ...metadata } = value;
        (msgContent.data as any).value = {
          ...metadata,
          bytes: toUint8Array(msg.buffers?.[0]),
        };
      }
    }
    try {
      // Store synchronization starts after unpackView has registered the application manager
      this.appManager?.handle_comm_message(msg);
    } catch (e) {
      console.error("PRET: Error during comm message reception", e);
    }
  };

  /**
   * Register a new kernel
   */
  handleKernelChanged = ({
    name,
    oldValue,
    newValue,
  }: {
    name: string;
    oldValue: IKernelConnection | null;
    newValue: IKernelConnection | null;
  }) => {
    if (this.disposed) return;
    if (oldValue) {
      oldValue.removeCommTarget(this.commTargetName, this.handleCommOpen);
      this.stopConnection("PRET kernel changed.");
    }
    this.kernel = newValue;
    if (newValue) {
      newValue.registerCommTarget(this.commTargetName, this.handleCommOpen);
      if (this.consumers.size) void this.connectToAnyKernel();
    }
  };

  handleConnectionFailure = (error: Error) => {
    console.log("PRET: Connection failure", error);
    this.markDisconnected({
      reason: error.name,
      lastError: error.message,
    });
  };

  /**
   * Deserialize a view data to turn it into a callable js function
   * @param view_data
   */
  unpackView({ serialized, marshaler_id, chunk_idx }: PretViewData): any {
    if (!serialized) {
      throw new Error(
        `Missing serialized bundle for marshaler ${marshaler_id}`
      );
    }
    const [renderable, manager] = this.unpack(
      serialized,
      marshaler_id,
      chunk_idx
    );
    this.appManager = manager;
    this.propagateConnectionState({
      kernelConnectionStatus: this.getKernelConnectionStatus(),
    });
    this.appManager.register_environment_handler(this);
    this.propagateConnectionState({
      kernelConnectionStatus: this.getKernelConnectionStatus(),
    });
    return renderable;
  }

  async fetchBundle(
    marshalerId: string,
    minChunkIdx?: number
  ): Promise<PretSerialized> {
    if (this.disposed || this.consumers.size === 0) throw new Error("PRET has no active views.");
    const cached = this.bundleCache.get(marshalerId);
    if (
      cached &&
      (minChunkIdx === undefined || minChunkIdx <= cached.maxChunkIdx)
    ) {
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
    const byteOffset = cached ? cached.byteLength : 0;
    let resolveFn!: (value: BundleResponse) => void;
    let rejectFn!: (reason?: any) => void;
    const pending = new Promise<BundleResponse>((resolve, reject) => {
      resolveFn = resolve;
      rejectFn = reject;
    });
    this.bundleRequests.set(requestId, {
      resolve: resolveFn,
      reject: rejectFn,
      marshalerId,
      byteOffset,
    });
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
        this.context.sessionContext.session.kernel.connectionStatus !==
          "connected";
      error.code = disconnected ? "PRET_JUPYTER_DOWN" : "PRET_BUNDLE_TIMEOUT";
      if (disconnected) {
        this.markDisconnected({
          reason: "bundle_request_connection_down",
          lastError: error.message,
        });
      }
      pendingRequest.reject(error);
    }, PRET_BUNDLE_REQUEST_TIMEOUT_MS);

    const generation = this.generation;
    const inflight = pending
      .then((bundle) => {
        if (generation !== this.generation) throw new Error("PRET view connection released.");
        this.bundleCache.set(marshalerId, bundle);
        return bundle;
      })
      .finally(() => {
        clearTimeout(timeout);
        if (this.bundleInFlight.get(marshalerId) === inflight) this.bundleInFlight.delete(marshalerId);
      });
    this.bundleInFlight.set(marshalerId, inflight);

    if (!this.context?.sessionContext.session?.kernel) {
      this.bundleRequests.delete(requestId);
      this.bundleInFlight.delete(marshalerId);
      clearTimeout(timeout);
      const error: any = new Error(
        "No active kernel is available for this notebook."
      );
      error.code = "PRET_KERNEL_DOWN";
      throw error;
    }
    try {
      await this.sendMessage("bundle_request", {
        marshaler_id: marshalerId,
        request_id: requestId,
        byte_offset: byteOffset,
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
    await this.waitUntilReady();
    const serialized = await this.fetchBundle(
      viewData.marshaler_id,
      viewData.chunk_idx
    );
    this.markBackendMessageReceived("bundle_fetched");
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
