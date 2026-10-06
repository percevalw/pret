import * as arrow from "apache-arrow";
import { createOfflineDataFrame } from "../../client/dataframe";
import { decodeDataFrameRows } from "../../client/dataframeValueConversions";
import { attachStore, beginTransaction, createStoreDoc, getStoreHistory, snapshot, subscribe } from "../../client/store";

// Check that ordinary fields and two local tables share transactions, and connection handling of connected Stores
export async function runStoreSessionTests(): Promise<void> {
  const check = (condition, name) => {
    if (!condition) throw new Error(name);
  };
  const doc = createStoreDoc({ data: { todos: { milk: false, report: false } }, metadata: {}, id: "todos" }, 1);
  const table = new arrow.Table({ label: arrow.vectorFromArray(["old"], new arrow.Utf8()) });
  const docs = createOfflineDataFrame("docs", "docs-binding", arrow.tableToIPC(table));
  const other = createOfflineDataFrame("other", "other-binding", arrow.tableToIPC(table));
  const controller = attachStore(doc, { docs, other }, null, "michael");
  const store = doc.proxyAt(["data"]);
  await controller.ready;
  const label = async (resource) => decodeDataFrameRows((await resource.query({})).bytes)[0].label;
  try {
    const [, end] = beginTransaction(store);
    store.todos.milk = true;
    const first = docs.apply_edit({ kind: "update", row_identity: 1, values: { label: "edited" } });
    const second = other.apply_edit({ kind: "update", row_identity: 1, values: { label: "also edited" } });
    end();
    await Promise.all([first, second]);
    check(
      store.todos.milk && (await label(docs)) === "edited" && (await label(other)) === "also edited",
      "one explicit transaction commits ordinary state and both tables"
    );
    check(!controller.canUndo && !(await controller.undo()), "browser only Stores have no undo");
    try {
      docs.apply_edit({ kind: "update", row_identity: 1, values: { missing: "invalid" } });
    } catch {}
    await Promise.resolve();
    check((await label(docs)) === "edited" && !controller.getSnapshot().pending, "invalid table edits change nothing");
    check(
      getStoreHistory(docs) === controller && getStoreHistory({}) === null,
      "history lookup works for resources and optional unmanaged values"
    );
    store.todos.report = 2;
    const [, finish] = beginTransaction(store);
    store.todos.report = 3;
    const [, finishNested] = beginTransaction(store);
    const aborted = docs.apply_edit({ kind: "update", row_identity: 1, values: { label: "aborted" } });
    finishNested("ValueError", new Error("context failed"));
    finish();
    await aborted;
    await Promise.resolve();
    check(
      store.todos.report === 2 && (await label(docs)) === "edited",
      "nested context exceptions abort ordinary and table edits"
    );
  } finally {
    controller.close();
  }
  checkProxies(check);

  // Comm replies can change the connection reason while the transport stays connected
  for (const mode of ["paged", "full"] as const) {
    const page = createStoreDoc({ data: {}, metadata: {}, id: "reader" }, 0);
    let requests = 0;
    const history = attachStore(
      page,
      {},
      async () => ({}),
      "reader",
      async (revision) => {
        requests++;
        return {
          revision: 1,
          tables: [],
          commits: [],
          state: revision === undefined ? { data: { value: 1 }, metadata: {}, id: "reader" } : null,
          bytes: new Uint8Array(),
        };
      },
      mode
    );
    try {
      await history.ready;
      const initialRequests = requests;
      for (let reply = 0; reply < 10; reply++) {
        history.setConnected(true);
        await new Promise((resolve) => setTimeout(resolve, 0));
      }
      check(requests === initialRequests, `${mode} Store ignores repeated connected notifications from comm replies`);
      history.setConnected(false);
      history.setConnected(true);
      await new Promise((resolve) => setTimeout(resolve, 0));
      check(requests === initialRequests + 1, `${mode} Store fetches missed changes once after reconnecting`);
      check(page.proxyAt(["data", "value"]) === 1, `${mode} Store shows the values Python sent`);
    } finally {
      history.close();
    }
  }

  // Without Python, eg on a static documentation page, a paged Store shows the values sent with the page
  let online = false;
  const offlinePage = createStoreDoc({ data: { count: 1 }, metadata: {}, id: "offline" }, 3);
  const offline = attachStore(
    offlinePage,
    {},
    async () => ({}),
    "offline",
    async () => {
      if (!online) throw new Error("Could not communicate with server");
      const ops = [{ op: "set", path: ["data", "count"], value: 2 }];
      const commits = [{ revision: 4, request_id: "edit", origin: "python", kind: "edit", ops, changes: [], summaries: [] }];
      return { revision: 4, tables: [], commits, state: null, bytes: new Uint8Array() };
    },
    "paged"
  );
  try {
    await offline.ready;
    const counter = offlinePage.proxyAt(["data"]);
    check(counter.count === 1 && !offline.getSnapshot().connected, "a Store without Python shows the values of the page");
    let refused = false;
    try {
      counter.count = 5;
    } catch {
      refused = true;
    }
    check(refused && counter.count === 1, "a Store without Python refuses edits");
    online = true;
    offline.setConnected(true);
    await new Promise((resolve) => setTimeout(resolve, 0));
    check(counter.count === 2, "a Store fetches the later commits once Python is reachable");
  } finally {
    offline.close();
  }
}

// Check the proxies compiled Python uses, snapshot sharing, captured items, rollback and subscriptions
function checkProxies(check: (condition: unknown, name: string) => void): void {
  const doc = createStoreDoc(
    { data: { todos: [{ text: "milk" }, { text: "eggs" }], filters: { label: "all" } }, metadata: {}, id: "proxies" },
    1
  );
  const store = doc.proxyAt(["data"]);
  const events = [];
  const unsubscribe = subscribe(store.todos, (ops) => events.push(...ops));
  const eggs = store.todos[1];
  const filters = snapshot(store.filters);
  const before = snapshot(store.todos);
  check(store.todos.push({ text: "tea" }) === 3, "push returns the new length");
  check(snapshot(store.filters) === filters, "untouched values keep their snapshot");
  check(snapshot(store.todos) !== before && before.length === 2, "edited values get a new snapshot");
  doc.applyRemote([{ op: "insert", path: ["data", "todos", 0], value: { text: "bread" } }], { origin: "peer" });
  eggs.done = true;
  check(snapshot(store.todos)[2].done === true && store.todos[2] === eggs, "a captured item follows its value");
  const removed = store.todos.splice(0, 2, { text: "jam" });
  check(removed.length === 2 && removed[0].text === "bread", "splice returns the removed values");
  check(("label" in store.filters) === true && Object.prototype.hasOwnProperty.call(store.filters, "missing") === false,
    "maps only report their own keys");
  delete store.todos[0];
  check(store.todos.length === 2 && store.todos[0] === eggs, "deleting a list index shifts the following items");
  const [, end] = beginTransaction(store);
  store.filters.label = "done";
  store.todos.length = 0;
  end("ValueError", new Error("failed"));
  check(store.filters.label === "all" && store.todos[0] === eggs, "a failed transaction restores the same values");
  check(events.length && events.every((op) => typeof op.path[0] === "number"), "subscribers receive relative paths");
  unsubscribe();
  store.filters.row = { id: 12n };
  check(snapshot(store.filters).row.id === 12, "DataFrame BigInt values become numbers");
  doc.applyRemote([{ op: "set", path: ["data", "count"], value: 2n ** 40n }], { origin: "python" });
  check(store.count === 2 ** 40, "64 bit integers sent by Python become numbers");
}
