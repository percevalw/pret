import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { loadClient } from "./browser";
const { test } = require(process.env.PRET_TESTING_HELPER);

test("SQLite queries preserve exact Arrow types and pending local edits roll back", async ({ page }) => {
  execFileSync(process.env.PYTHON, [path.join(__dirname, "fixtures.py"), process.env.PRET_TEST_ROOT]);
  await loadClient(page, ['export {runDataFrameTests} from "./tests/client/dataframe.browser";']);
  const bytes = JSON.parse(fs.readFileSync(path.join(process.env.PRET_TEST_ROOT, "typed.json"), "utf8"));
  await page.evaluate(async (bytes) => {
    const { runDataFrameTests } = await import("/pret-client.mjs");
    return runDataFrameTests(bytes);
  }, bytes);
});
