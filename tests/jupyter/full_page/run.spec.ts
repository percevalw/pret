let { expect, test } = require("@jupyterlab/galata");
const { createDirectoryResetController } = require("../reset-test-dir");

// JUPYTERLAB_VERSION is set in run.sh
if (process.env.JUPYTERLAB_VERSION < "4") {
  console.log("Using old galata to match JupyterLab 3");
  const oldGalata = require("old-galata");
  expect = oldGalata.expect;
  test = oldGalata.test;
}

const directoryReset = createDirectoryResetController(__dirname);

test.describe.configure({ mode: "serial" });

test.beforeAll(async () => {
  await directoryReset.prepare();
  await directoryReset.reset();
});

test.beforeEach(async () => {
  await directoryReset.reset();
});

test.afterEach(async ({ page }, testInfo) => {
  if (testInfo.status !== testInfo.expectedStatus) {
    // Get a unique place for the screenshot.
    const screenshotPath = testInfo.outputPath(`failure.png`);
    // Add it to the report.
    testInfo.attachments.push({
      name: "screenshot",
      path: screenshotPath,
      contentType: "image/png",
    });
    try {
      // Take the screenshot itself.
      await page.screenshot({ path: screenshotPath, timeout: 1000 });
    } catch (e) {
      console.error("Could not create screenshot");
    }
  }

  await directoryReset.reset();
});

test.afterAll(async () => {
  await directoryReset.dispose();
});

test.describe("Notebook Tests", () => {
  test("TodoHTML Full Page", async ({ page }) => {
    const notebookUrl = new URL(
      "/doc/tree/full_page/TodoHTML.ipynb?pret-fullpage-cell=1",
      page.url()
    ).toString();

    // Galata route mocks break direct URL access, so we undo it here
    await page.unrouteAll({ behavior: "ignoreErrors" });
    await page.page.goto(notebookUrl, { waitUntil: "domcontentloaded" });

    const fullPageHost = page.locator("#pret-fullpage-host");
    await fullPageHost.waitFor({ state: "visible" });

    const reloadButton = fullPageHost.locator(".pret-restart-app-button");
    await reloadButton.waitFor({ state: "visible" });
    await reloadButton.click();
    await expect(page.locator("#main")).toBeHidden();

    const todoApp = page.locator("#todoapp");
    try {
      await todoApp.waitFor({ state: "visible", timeout: 20000 });
    } catch (e) {
      await reloadButton.click();
      await expect(page.locator("#main")).toBeHidden();
      await todoApp.waitFor({ state: "visible" });
    }

    await expect(page.locator("#main")).toBeHidden();
    const pageScreenshot = await page.screenshot();
    const fullPageHostScreenshot = await page
      .locator("#pret-fullpage-host")
      .screenshot();
    expect(fullPageHostScreenshot).toEqual(pageScreenshot);
  });
});
