import assert from "node:assert/strict";
import * as arrow from "apache-arrow";
import { loadDataFrame } from "./dataframeNode";
const { test } = require(process.env.PRET_TESTING_HELPER);

// React renders that are discarded or replaced must not leave DataFrame subscriptions behind, and a new filter
// reports its loading while the previous rows stay shown
test("shares one client and polls only while widgets are mounted", async () => {
  const { JSDOM } = require("jsdom");
  const dom = new JSDOM('<!doctype html><div id="root"></div>', {
    pretendToBeVisual: true,
  });
  const originalGlobals = Object.fromEntries(
    ["window", "document", "navigator", "MessageChannel"].map((key) => [
      key,
      Object.getOwnPropertyDescriptor(global, key),
    ])
  );
  for (const [key, value] of Object.entries({
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    MessageChannel: undefined,
  }))
    Object.defineProperty(global, key, { value, configurable: true, writable: true });
  const React = require("react");
  const ReactDOM = require("react-dom");
  const { act } = require("react-dom/test-utils");
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
    const runtimeModule = loadDataFrame();
    const { useDataFrameView } = runtimeModule;
    const dataframe = runtimeModule.createClientDataFrame({
      resource_id: "documents",
      binding_id: "view",
      describe: async () => ({
        columns: [],
        capabilities: { changes: true },
        revision: 0,
      }),
      query: async (options) => {
        if (options?.filter?.value?.[0] === "rejected") throw new Error("Text filters require a string column");
        return {
          bytes: arrow.tableToIPC(arrow.tableFromArrays({ id: [] })),
          row_count: 0,
          total_count: 0,
          revision: 0,
          offset: 0,
        };
      },
    });
    assert.equal(runtimeModule.createClientDataFrame({ resource_id: "documents", binding_id: "view" }), dataframe);
    let widgetState;
    // Capture the active row source while React mounts and replaces the resource
    function Widget({ source = dataframe, filter = null }) {
      widgetState = useDataFrameView(source, filter, "id");
      return null;
    }
    const root = document.getElementById("root");
    await act(async () => {
      ReactDOM.render(React.createElement(React.StrictMode, null, React.createElement(Widget)), root);
      await new Promise(setImmediate);
    });
    assert.equal(timers.size, 1);
    await act(async () => {
      ReactDOM.render(React.createElement(React.StrictMode, null, React.createElement(Widget)), root);
      await new Promise(setImmediate);
    });
    await act(async () => {
      ReactDOM.unmountComponentAtNode(root);
      await new Promise(setImmediate);
    });
    assert.equal(timers.size, 0);
    await act(async () => {
      ReactDOM.render(React.createElement(Widget), root);
      await new Promise(setImmediate);
    });
    assert.equal(timers.size, 1);
    assert.ok(widgetState.rowSource);
    const previous = widgetState.rowSource;
    const filter = { kind: "predicate", column: "id", operator: "in", value: [1] };
    act(() => {
      ReactDOM.render(React.createElement(Widget, { filter }), root);
    });
    assert.equal(widgetState.loading, "Loading data");
    assert.equal(widgetState.previousRowSource, previous);
    await act(async () => {
      await new Promise(setImmediate);
    });
    assert.equal(widgetState.loading, null);
    assert.notEqual(widgetState.rowSource, previous);
    // A filter the source rejects is reported by this view only, not by the other widgets of the DataFrame
    act(() => {
      ReactDOM.render(React.createElement(Widget, { filter: { ...filter, value: ["rejected"] } }), root);
    });
    await act(async () => {
      await new Promise(setImmediate);
    });
    assert.deepEqual(
      [widgetState.queryError, widgetState.error, widgetState.loading],
      ["Text filters require a string column", null, null]
    );
    await act(async () => {
      ReactDOM.render(React.createElement(Widget, { source: null }), root);
      await new Promise(setImmediate);
    });
    assert.equal(widgetState.rowSource, null);
    assert.equal(timers.size, 0);
    await act(async () => ReactDOM.unmountComponentAtNode(root));
  } finally {
    Object.assign(global, originalTimers);
    dom.window.close();
    for (const [key, descriptor] of Object.entries(originalGlobals)) {
      if (descriptor) Object.defineProperty(global, key, descriptor);
      else delete global[key];
    }
  }
});
