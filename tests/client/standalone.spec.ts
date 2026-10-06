import fs from "node:fs";
import path from "node:path";
const { test, expect } = require(process.env.PRET_TESTING_HELPER);

test("Standalone documentation pages without widgets do not load a bundle", async ({ page }) => {
  const assets = path.resolve(__dirname, "../../pret/js-base");
  const template = fs.readFileSync(path.join(assets, "index.html"), "utf8");
  const script = template.match(/src="([^"]*bundle[^"]*\.js)"/)[1];
  const errors = [];
  const requests = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => requests.push(request.url()));
  await page.route("http://localhost/**", (route) => {
    const url = new URL(route.request().url());
    return url.pathname === "/"
      ? route.fulfill({
          contentType: "text/html",
          body: `<!doctype html><body><h1>Documentation</h1>
            <script>
              window.document$ = {subscribe(render) {window.renderDocumentation = render; render();}};
            </script>
            <script src="${script}"></script></body>`,
        })
      : route.fulfill({ path: path.join(assets, path.basename(url.pathname)) });
  });
  await page.goto("http://localhost/");
  await page.waitForFunction(() => typeof window.renderDocumentation === "function");
  // Material also calls the renderer after navigating to a page without widgets
  await page.evaluate(async () => {
    window.renderDocumentation();
    await new Promise(requestAnimationFrame);
  });
  expect(errors).toEqual([]);
  expect(requests.filter((url) => !url.endsWith("/") && !url.endsWith(".js"))).toEqual([]);
});
