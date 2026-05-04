let { expect, test } = require("@jupyterlab/galata");
const path = require("path");
const fs = require("fs");
const childProcess = require("child_process");
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
  test("TodoHTML Recover Scenario", async ({ page, tmpPath }) => {
    const activePanel = ".jp-NotebookPanel:not(.lm-mod-hidden)";
    const executionIndicator = page.locator(
      `${activePanel} .jp-Notebook-ExecutionIndicator`
    );
    const fileName = "recover/TodoHTML.ipynb";
    const name = path.basename(fileName);
    await page.notebook.openByPath(fileName);
    await page.waitForTimeout(1000);

    await page.notebook.activate(name);
    await page.waitForTimeout(1000);

    await page.waitForSelector(`${activePanel} .pret-view`);
    await page.waitForSelector(`${activePanel} .pret-inline-fallback`, {
      state: "attached",
    });
    const restartBtn = await page.waitForSelector(
      `${activePanel} .pret-restart-app-button`
    );
    expect(restartBtn).toBeTruthy();
    restartBtn.click();

    // Check that input checkbox "faire à manger" is present
    await page.notebook.activate(name);
    await page.waitForSelector(`${activePanel} .pret-view`);
    await page.waitForSelector(`${activePanel} #faire-à-manger`, {
      state: "attached",
    });
    const checkbox = page.locator("#faire-à-manger");
    expect(checkbox).toBeChecked();
    let desc = await page.waitForSelector(`${activePanel} .pret-view p`, {
      state: "attached",
    });
    let expectedDescText = "Number of unfinished todo: 1";
    expect(await desc.textContent()).toEqual(expectedDescText);

    // Go offline
    await expect(executionIndicator).not.toHaveAttribute(
      "data-status",
      "disconnected"
    );
    await page.context().setOffline(true);
    await page.waitForTimeout(10000);
    await page.waitForSelector(
      `${activePanel} .jp-Notebook-ExecutionIndicator[data-status=disconnected], ${activePanel} .jp-Notebook-ExecutionIndicator[data-status=connecting], .jp-Dialog`
    );

    // Try to ncheck the checkbox
    await checkbox.dispatchEvent("click");
    await page.waitForTimeout(1000);

    // The checkbox was checked before, it should stay checked when we're
    // offline and the user tries to interact with it
    await page.waitForSelector(`${activePanel} #faire-à-manger:checked`);
    // Checkbox might be obscured by jupyter connection error popup
    await checkbox.dispatchEvent("click");
    await page.waitForTimeout(1000);
    expect(checkbox).toBeChecked();

    // Go online
    // don't check for idle because it doesn't work on chromium, but it should
    // await expect(executionIndicator).not.toHaveAttribute("data-status", "idle");
    await page.context().setOffline(false);
    await page.waitForTimeout(10000);
    await page.waitForSelector(
      `${activePanel} .jp-Notebook-ExecutionIndicator[data-status=idle]`
    );

    // Uncheck the checkbox
    await checkbox.dispatchEvent("click");
    await page.waitForTimeout(1000);
    await page.waitForSelector(`${activePanel} #faire-à-manger:not(:checked)`);
    await expect(checkbox).not.toBeChecked();
  });
});
