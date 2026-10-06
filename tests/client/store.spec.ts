import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { loadClient } from "./browser";
const { test, expect } = require(process.env.PRET_TESTING_HELPER);

test("Python standalone exports edit without unresolved generated source maps", async ({
  page,
  browserName,
}) => {
  execFileSync(process.env.PYTHON, [path.join(__dirname, "fixtures.py"), process.env.PRET_TEST_ROOT]);
  // Plain HTTP keeps getRandomValues available but does not expose randomUUID
  await page.addInitScript(() => {
    Object.defineProperty(crypto, "randomUUID", { value: undefined, configurable: true });
  });
  await loadClient(page, [
    'export * from "./client/store";',
    'export {decodeDataFrameRows} from "./client/dataframeValueConversions";',
    'export {makeLoadApp} from "./client/appLoader";',
  ]);
  const sourceMaps = [];
  const client = browserName === "chromium" ? await page.context().newCDPSession(page) : null;
  client?.on("Debugger.scriptParsed", (script) => {
    if (script.url.endsWith("dynamic_factory.js") || script.url.startsWith("pret-coverage:")) {
      sourceMaps.push(script.sourceMapURL);
    }
  });
  await client?.send("Debugger.enable");
  await page.evaluate(async (serialized) => {
    const lib = await import("/pret-client.mjs");

    const bytes = Uint8Array.from(atob(serialized.bytes), (value) => value.charCodeAt(0));
    const cachedCode = serialized.code + "\n//# sourceMappingURL=__main__.map";
    const store = lib.makeLoadApp()([bytes, cachedCode], serialized.id, serialized.chunk);
    const history = lib.getStoreHistory(store);
    await history.ready;
    store.todo.milk = true;
    await store.rows.apply_edit({
      base_revision: history.getSnapshot().revision,
      row_identity: 1,
      values: { label: "edited" },
    });
    const rows = lib.decodeDataFrameRows((await store.rows.query({})).bytes);
    if (rows[0].label !== "edited" || !store.todo.milk) throw new Error("Marshaled standalone Store must edit locally");
    if ((await history.undo()) || history.canUndo) throw new Error("Standalone Stores have no undo");
    return "Python marshaling and standalone browser editing passed";
  }, JSON.parse(fs.readFileSync(path.join(process.env.PRET_TEST_ROOT, "standalone.json"), "utf8")));
  if (client) {
    expect(sourceMaps.length).toBeGreaterThan(0);
    expect(sourceMaps.every((value) => !value)).toBe(true);
    await client.detach();
  }
});
