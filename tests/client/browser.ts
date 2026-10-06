import fs from "node:fs";
import path from "node:path";
import esbuild from "esbuild";

// Serve compiled client imports and SQLite to the Playwright page without a second test server
export async function loadClient(page, exports) {
  const root = path.resolve(__dirname, "../..");
  const built = await esbuild.build({
    stdin: { contents: exports.join("\n"), resolveDir: root },
    bundle: true,
    outfile: path.join(process.env.PRET_TEST_ROOT, "pret-client.mjs"),
    format: "esm",
    platform: "browser",
    sourcemap: process.env.PRET_COVERAGE_DIR ? "inline" : false,
    treeShaking: process.env.PRET_COVERAGE_DIR ? false : undefined,
    write: false,
  });
  await page.route("**/pret-client.mjs", (route) =>
    route.fulfill({
      contentType: "text/javascript",
      body: built.outputFiles[0].text,
    })
  );
  await page.route("**/sqlite3.wasm", (route) =>
    route.fulfill({
      contentType: "application/wasm",
      body: fs.readFileSync(path.join(root, "node_modules/@sqlite.org/sqlite-wasm/dist/sqlite3.wasm")),
    })
  );
  if (!page.url().startsWith("http")) {
    await page.route("http://localhost/", (route) =>
      route.fulfill({
        contentType: "text/html",
        body: "<!doctype html><title>PRET client</title>",
      })
    );
    await page.goto("http://localhost/");
  }
}
