const assert = require("node:assert/strict");
const vm = require("node:vm");
const path = require("node:path");
const esbuild = require("esbuild");
const { test } = require(process.env.PRET_TESTING_HELPER);

// Test the session manager with controlled Jupyter signals and comm replies
test("Jupyter manager shares comms and ignores late discovery after release", async () => {
  const root = path.resolve(__dirname, "../..");
  const built = await esbuild.build({
    sourcemap: process.env.PRET_COVERAGE_DIR ? "inline" : false,
    treeShaking: process.env.PRET_COVERAGE_DIR ? false : undefined,
    entryPoints: [path.join(root, "client/jupyter/manager.ts")],
    bundle: true,
    platform: "node",
    format: "cjs",
    write: false,
    plugins: [
      {
        name: "view-loader",
        setup(build) {
          build.onResolve({ filter: /appLoader$/ }, () => ({ path: "loader", namespace: "fixture" }));
          build.onLoad({ filter: /.*/, namespace: "fixture" }, () => ({
            contents: `export const makeLoadApp = () => () => [() => null, {
              register_environment_handler() {}, set_connection_status() {},
              handle_comm_message(msg) { globalThis.delivered.push(msg.content.data.method); }
            }];`,
          }));
        },
      },
    ],
  });
  const timers = new Map();
  let sequence = 0;
  const window = {
    setTimeout(fn, delay) {
      const id = ++sequence;
      timers.set(id, { fn, delay });
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
  };
  const delivered = [];
  const module = { exports: {} };
  vm.runInNewContext(
    built.outputFiles[0].text,
    {
      module,
      delivered,
      exports: module.exports,
      require,
      window,
      console: { info() {}, warn() {}, error() {}, log() {} },
      process,
      setTimeout,
      clearTimeout,
      TextEncoder,
      TextDecoder,
      Uint8Array,
      ArrayBuffer,
    },
    { filename: path.join(root, "client/jupyter/manager.coverage.js") }
  );
  const Manager = module.exports.default;
  const flush = async () => {
    for (let i = 0; i < 20; i++) await Promise.resolve();
  };
  const tick = async () => {
    const ready = [...timers].filter(([, t]) => t.delay <= 3000);
    for (const [id, t] of ready) if (timers.delete(id)) t.fn();
    await flush();
  };
  class Signal {
    slots = new Set();
    connect(fn) {
      this.slots.add(fn);
    }
    disconnect(fn) {
      this.slots.delete(fn);
    }
    emit(args) {
      for (const fn of this.slots) fn(this, args);
    }
  }
  // The kernel rejects frontend opens until Python registers PRET
  function session() {
    const comms = new Map();
    const kernel = {
      id: "kernel",
      connectionStatus: "connected",
      handleComms: true,
      backend: false,
      infoCalls: 0,
      opens: 0,
      closes: 0,
      infoReply: null,
      target: null,
      registerCommTarget(name, fn) {
        this.target = fn;
      },
      removeCommTarget(name, fn) {
        if (this.target === fn) this.target = null;
      },
      async requestCommInfo() {
        this.infoCalls++;
        if (this.infoReply) return this.infoReply;
        return {
          content: {
            status: "ok",
            comms: Object.fromEntries([...comms.keys()].map((id) => [id, { target_name: "pret" }])),
          },
        };
      },
      hasComm(id) {
        return comms.has(id);
      },
      _comms: comms,
      createComm(name, id = "comm-" + ++sequence) {
        const comm = {
          commId: id,
          onMsg: null,
          onClose: null,
          open: () => {
            this.opens++;
            if (!this.backend) queueMicrotask(() => comm.onClose?.());
            else comms.set(id, comm);
          },
          send: ({ method, data }) => {
            if (method === "is_alive_request" && this.backend)
              queueMicrotask(() =>
                comm.onMsg?.({
                  content: {
                    data: {
                      method: "is_alive_response",
                      data: { request_id: data.request_id, ok: true, protocol_version: 2 },
                    },
                  },
                })
              );
          },
          close: () => {
            this.closes++;
            comms.delete(id);
            comm.onClose?.();
          },
        };
        return comm;
      },
    };
    const context = { sessionContext: { session: { kernel }, ready: Promise.resolve(), kernelChanged: new Signal() } };
    const services = { connectionFailure: new Signal() };
    return { kernel, context, services, manager: new Manager(context, services) };
  }

  const { kernel, manager, context, services } = session();
  await flush();
  await tick();
  assert.equal(kernel.infoCalls, 0, "ordinary notebook stays passive");
  const releaseFirst = manager.acquire();
  const releaseSecond = manager.acquire();
  const failed = manager.waitUntilReady().then(
    () => false,
    () => true
  );
  await flush();
  assert.equal(await failed, true);
  assert.equal(kernel.opens, 1, "concurrent consumers share the rejected handshake");
  for (let i = 0; i < 3; i++) await tick();
  assert.equal(kernel.opens, 1, "rejected initial opens do not poll");
  kernel.backend = true;
  const backend = kernel.createComm("pret");
  backend.open();
  kernel.target(backend);
  await flush();
  await manager.waitUntilReady();
  releaseFirst();
  await tick();
  assert.equal(kernel.closes, 0, "one view cannot close a shared comm");
  backend.close();
  await tick();
  await manager.waitUntilReady();
  assert.equal(kernel.opens, 3, "an established connection recovers while a view remains");
  manager.unpackView({ serialized: [], marshaler_id: "fixture", chunk_idx: 0 });
  const connectedComm = [...kernel._comms.values()].at(-1);
  releaseSecond();
  connectedComm.onMsg({ content: { data: { method: "call_success", data: { value: 1 } } } });
  assert.deepEqual(delivered, ["call_success"], "idle sessions still deliver responses to completed edits");
  releaseSecond();
  await tick();
  const calls = kernel.infoCalls;
  services.connectionFailure.emit(new Error("network"));
  await tick();
  assert.equal(kernel.infoCalls, calls, "no recovery after final consumer closes");
  assert.equal(timers.size, 0);
  const releaseAgain = manager.acquire();
  await flush();
  await manager.waitUntilReady();
  releaseAgain();
  manager.dispose();
  assert.equal(context.sessionContext.kernelChanged.slots.size, 0);
  assert.equal(services.connectionFailure.slots.size, 0);
  assert.equal(kernel.target, null);

  const late = session();
  let reply;
  late.kernel.infoReply = new Promise((resolve) => {
    reply = resolve;
  });
  const release = late.manager.acquire();
  const stopped = late.manager.waitUntilReady().then(
    () => false,
    () => true
  );
  await flush();
  release();
  reply({ content: { status: "ok", comms: {} } });
  await flush();
  await tick();
  assert.equal(await stopped, true);
  assert.equal(late.kernel.opens, 0, "late discovery cannot reopen a released connection");
  late.manager.dispose();
  assert.equal(timers.size, 0);

  const retry = session();
  const releaseRetry = retry.manager.acquire();
  await flush();
  retry.kernel.backend = true;
  await retry.manager.connectToAnyKernel();
  await retry.manager.waitUntilReady();
  assert.equal(retry.kernel.opens, 2, "explicit reopening retries a rejected handshake");
  releaseRetry();
  retry.manager.dispose();

  const changed = session();
  let oldReply;
  changed.kernel.infoReply = new Promise((resolve) => {
    oldReply = resolve;
  });
  const releaseChanged = changed.manager.acquire();
  await flush();
  const replacement = session();
  replacement.manager.dispose();
  replacement.kernel.backend = true;
  changed.context.sessionContext.session.kernel = replacement.kernel;
  changed.context.sessionContext.kernelChanged.emit({
    name: "kernel",
    oldValue: changed.kernel,
    newValue: replacement.kernel,
  });
  await flush();
  await changed.manager.waitUntilReady();
  oldReply({ content: { status: "ok", comms: {} } });
  await flush();
  assert.equal(changed.kernel.opens, 0);
  assert.equal(replacement.kernel.opens, 1, "old kernel discovery cannot replace the current comm");
  releaseChanged();
  changed.manager.dispose();
  assert.equal(timers.size, 0);
});
