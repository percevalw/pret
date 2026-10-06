import assert from "node:assert/strict";
import * as arrow from "apache-arrow";
import { loadDataFrame } from "./dataframeNode";
const { ClientDataFrame } = loadDataFrame();
const { test } = require(process.env.PRET_TESTING_HELPER);

// Encode source replies so cache tests use the same Arrow decoder as notebook widgets
function encodeRows(rows) {
  const keys = Object.keys(rows[0] || { id: 0 });
  return arrow.tableToIPC(
    new arrow.Table(
      Object.fromEntries(
        keys.map((key) => [
          key,
          arrow.vectorFromArray(
            rows.map((row) => row[key]),
            typeof rows[0]?.[key] === "string" ? new arrow.Utf8() : new arrow.Float64()
          ),
        ])
      )
    )
  );
}

// Fetch selected rows by ID even when they are outside the first page
test("queries sorted pages and selected identities without downloading all keys", async () => {
  const data = Array.from({ length: 1000 }, (_, index) => ({ id: `row-${index}`, count: index }));
  const calls = [];
  const dataframe = {
    describe: async () => ({ columns: [], capabilities: {}, revision: 0, internal_id_col: "id" }),
    resolve_identity: (key) => (key === "pending" ? "row-100" : key),
    query: async (options) => {
      calls.push(options);
      let rows = data
        .filter((row) => row.count >= 100)
        .slice()
        .reverse();
      if (options.filter?.kind === "and") {
        const keys = options.filter.children[1].value;
        rows = rows.filter((row) => keys.includes(row.id));
      }
      return {
        bytes: encodeRows(rows.slice(options.offset ?? 0, (options.offset ?? 0) + options.limit)),
        total_count: rows.length,
        revision: 0,
        positions: options.locate?.values.map((key) => rows.findIndex((row) => row.id === key)),
      };
    },
  };
  const runtime = new ClientDataFrame(dataframe);
  await runtime.ensureScope();
  const filter = { kind: "predicate", column: "count", operator: "gte", value: 100 };
  const active = await runtime.createView(filter, JSON.stringify(filter), "id", [
    { column: "count", direction: "desc" },
  ]);
  const source = active;
  const signal = new AbortController().signal;
  assert.equal(source.rowCount, 900);
  assert.equal((await source.getRows(0, 2, signal))[0].id, "row-999");
  assert.equal(await source.findRowIndex("row-100", signal), 899);
  assert.equal(await source.findRowIndex("pending", signal), 899);
  assert.equal((await source.getRowsByKeys(["pending"], signal))[0].id, "row-100");
  assert.deepEqual(
    Array.from(await source.getRowsByKeys(["row-100", "row-101"], signal), (row) => row.id),
    ["row-100", "row-101"]
  );
  assert.ok(calls.every((options) => options.limit <= 256));
  assert.equal(calls.at(-1).sort[0].direction, "desc");
  assert.deepEqual(
    Array.from(await source.getRowsAtIndices([899, 0, 899], signal), (row) => row.id),
    ["row-100", "row-999", "row-100"]
  );
});

// Fast scrolling must share pending rows and skip obsolete ranges before they reach the source
test("coalesces scroll requests and reuses cached rows", async () => {
  const calls = [];
  let release;
  const waiting = new Promise((resolve) => {
    release = resolve;
  });
  const dataframe = new ClientDataFrame({
    describe: async () => ({ columns: [], capabilities: {}, revision: 0, internal_id_col: "id" }),
    query: async (options) => {
      calls.push(options);
      if (options.limit) await waiting;
      return {
        total_count: 1000,
        revision: 0,
        bytes: encodeRows(Array.from({ length: options.limit }, (_, idx) => ({ id: (options.offset ?? 0) + idx }))),
      };
    },
  });
  await dataframe.ensureScope();
  const view = await dataframe.createView(null, "null");
  assert.equal(calls.length, 1);
  let controller = new AbortController();
  const requests = [view.getRows(31, 33, controller.signal)];
  for (let offset = 32; offset <= 73; offset++) {
    controller.abort();
    controller = new AbortController();
    requests.push(view.getRows(offset, offset + 36, controller.signal));
  }
  assert.equal(calls.length, 2);
  release();
  const results = await Promise.all(requests);
  assert.ok(results.slice(0, -1).every((rows) => rows.length === 0));
  assert.deepEqual(
    Array.from(results.at(-1), (row) => row.id),
    Array.from({ length: 36 }, (_, idx) => 73 + idx)
  );
  assert.equal(calls.length, 2);
  await view.getRows(75, 105, controller.signal);
  assert.equal(calls.length, 2);
  await view.getRows(250, 270, controller.signal);
  assert.deepEqual(
    calls.slice(1).map(({ offset, limit }) => [offset, limit]),
    [
      [0, 256],
      [256, 256],
    ]
  );
  await view.getRows(501, 531, controller.signal);
  assert.deepEqual(calls.at(-1).offset, 512);
  assert.equal(calls.at(-1).limit, 256);

  const query = dataframe.query;
  dataframe.query = async (options) => {
    if (options.limit > 64) throw new Error("Source query page exceeds the byte limit, request fewer rows or columns");
    return query(options);
  };
  const small = await dataframe.createView(null, "null");
  assert.equal((await small.getRows(501, 502, controller.signal))[0].id, 501);
  assert.equal(calls.at(-1).offset, 448);
  assert.equal(calls.at(-1).limit, 64);
  await small.getRows(512, 531, controller.signal);
  assert.equal(calls.at(-1).limit, 64);
  dataframe.query = async () => {
    throw new Error("Source unavailable");
  };
  await assert.rejects(small.getRows(800, 801, controller.signal), /Source unavailable/);
  let attempts = 0;
  dataframe.query = async () => {
    attempts++;
    throw new Error("Source query page exceeds the byte limit, request fewer rows or columns");
  };
  const oversized = await dataframe.createView(null, "null");
  await assert.rejects(oversized.getRows(0, 1, controller.signal), /byte limit/);
  assert.equal(attempts, 9);
});

// Limited tables keep one source result and still resolve related IDs outside that result
test("limited tables expose the loaded rows and replace them on query", async () => {
  const data = Array.from({ length: 1000 }, (_, id) => ({ id }));
  const calls = [];
  const dataframe = new ClientDataFrame({
    describe: async () => ({ columns: [], capabilities: { stable_identity: false }, row_limit: 500, revision: 0 }),
    query: async (options) => {
      calls.push(options);
      assert.equal(options.offset ?? 0, 0);
      assert.ok(!options.sort?.length && !options.locate);
      const rows = (options.filter ? data.filter((row) => options.filter.value.includes(row.id)) : data).slice(
        0,
        options.limit
      );
      return { bytes: encodeRows(rows), total_count: null, row_count: rows.length, revision: 0 };
    },
  });
  await dataframe.ensureScope();
  const table = await dataframe.createView(null, "null", "id");
  const form = await dataframe.createView(null, "null", "id");
  const signal = new AbortController().signal;
  assert.equal(table.rowCount, 500);
  assert.equal((await table.getRows(499, 700, signal)).length, 1);
  assert.equal(await table.findRowIndex(999, signal), -1);
  assert.equal((await form.getRowsByKeys([10], signal))[0].id, 10);
  assert.equal(calls.length, 1);
  assert.equal((await form.getRowsByKeys([999], signal)).length, 0);
  assert.equal(calls.length, 1);
  dataframe.setScope(await dataframe.fetchScope({ kind: "predicate", value: [999] }));
  const filtered = await dataframe.createView(null, "null", "id");
  assert.equal(filtered.rowCount, 1);
  assert.equal((await filtered.getRows(0, 500, signal))[0].id, 999);
});

// A loader of related tables can set a scope while useDataFrameView loads the whole table
test("keeps a scope set by a loader while the whole table loads", async () => {
  // Replies of the whole table and of the loader filter, answered in the order chosen below
  const replies = {};
  const dataframe = new ClientDataFrame({
    describe: async () => ({ columns: [], capabilities: {}, revision: 0 }),
    query: (options) =>
      new Promise((resolve) => {
        const total_count = options.filter ? 1 : 10;
        replies[options.filter ? "loader" : "table"] = () => resolve({ bytes: encodeRows([]), total_count, revision: 0 });
      }),
  });
  const loading = dataframe.ensureScope();
  const fetching = dataframe.fetchScope({ kind: "predicate", column: "id", operator: "in", value: [1] });
  while (!replies.table || !replies.loader) await new Promise(setImmediate);
  replies.loader();
  dataframe.setScope(await fetching);
  replies.table();
  await loading;
  assert.equal(dataframe.scope.row_count, 1);
});

// Ignore late query replies after a view closes or its data changes
test("discards late pages and invalidates every view on a changed revision", async () => {
  let resolvePage;
  const runtime = new ClientDataFrame({
    describe: async () => ({ columns: [], capabilities: {}, revision: 0 }),
    query: async (options) =>
      options.limit === 0
        ? { bytes: encodeRows([]), total_count: 1, revision: 0 }
        : new Promise((resolve) => {
            resolvePage = resolve;
          }),
  });
  await runtime.ensureScope();
  const signal = new AbortController().signal;
  const first = await runtime.createView(null, "null", "id");
  const late = first.getRows(0, 1, signal);
  runtime.releaseView(first);
  resolvePage({ bytes: encodeRows([{ id: 1 }]), revision: 0 });
  assert.equal((await late).length, 0);
  assert.equal(first.getCachedRow(0), undefined);
  const second = await runtime.createView(null, "null", "id");
  const third = await runtime.createView(null, "null", "id");
  const changed = second.getRows(0, 1, signal);
  resolvePage({ bytes: encodeRows([{ id: 1 }]), revision: 1 });
  assert.equal((await changed).length, 0);
  assert.equal(second.disposed, true);
  assert.equal(third.disposed, true);
  assert.equal(runtime.revision, 1);
});

// Rejected edits expose their error, and Store notifications replace the poll for complete local tables
test("exposes rejected edits and follows Store notifications without polling", async () => {
  const timers = new Set();
  const originalTimers = { setInterval: global.setInterval, clearInterval: global.clearInterval };
  Object.assign(global, {
    setInterval: (fn) => {
      timers.add(fn);
      return fn;
    },
    clearInterval: (timer) => timers.delete(timer),
  });
  try {
    const runtime = new ClientDataFrame({
      apply_edit: async () => {
        throw new Error("invalid integer");
      },
    });
    runtime.description = {
      internal_id_col: "id",
      capabilities: { edit: true, changes: true },
    };
    assert.equal((await runtime.applyEdit({ id: 1 }, "count", "bad")).status, "error");
    assert.equal(runtime.error, "invalid integer");
    let changed;
    runtime.subscribeChanges = (listener) => {
      changed = listener;
      return () => {
        changed = null;
      };
    };
    runtime.get_changes_after_revision = async () => ({ revision: 1 });
    const unsubscribe = runtime.subscribe(() => {});
    assert.equal(timers.size, 0);
    changed();
    await new Promise(setImmediate);
    assert.equal(runtime.revision, 1);
    unsubscribe();
    assert.equal(changed, null);
  } finally {
    Object.assign(global, originalTimers);
  }
});
