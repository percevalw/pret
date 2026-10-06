const assert = require("node:assert/strict");
import { loadClient } from "./browser";
const { test } = require(process.env.PRET_TESTING_HELPER);

// Transpiled Python code sorts like Python, results are compared with those of CPython
test("Python sorted is stable, also reversed, and compares tuples item by item", async ({ page }) => {
  await loadClient(page, ['export {sorted} from "./client/org.transcrypt.__runtime__";']);
  const result = await page.evaluate(async () => {
    const { sorted } = await import("/pret-client.mjs");
    const rows = [...Array(20).keys()].map((i) => ({ i, group: i % 3 }));
    return {
      stable: sorted(rows, (row) => row.group).map((row) => row.i),
      reversed: sorted(rows, (row) => row.group, true).map((row) => row.i),
      tuples: sorted([[10, 1], [9, 2], [9, 1], [9]]),
      numbers: sorted([10, 9, 100]),
    };
  });
  // [r["i"] for r in sorted(rows, key=lambda r: r["group"])] and the same with reverse=True
  assert.deepEqual(result.stable, [0, 3, 6, 9, 12, 15, 18, 1, 4, 7, 10, 13, 16, 19, 2, 5, 8, 11, 14, 17]);
  assert.deepEqual(result.reversed, [2, 5, 8, 11, 14, 17, 1, 4, 7, 10, 13, 16, 19, 0, 3, 6, 9, 12, 15, 18]);
  assert.deepEqual(result.tuples, [[9], [9, 1], [9, 2], [10, 1]]);
  assert.deepEqual(result.numbers, [9, 10, 100]);
});
