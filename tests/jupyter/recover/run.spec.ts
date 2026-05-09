let { expect, test } = require("@jupyterlab/galata");
const path = require("path");
const fs = require("fs");
const childProcess = require("child_process");
const { createDirectoryResetController } = require("../utils");

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
    const fileName = "recover/TodoHTML.ipynb";
    const name = path.basename(fileName);
    await page.notebook.openByPath(fileName);
    const waitForPretConnection = (connected) =>
      page.waitForFunction(() => {
        const manager = (window as any).pretManager;
        return manager?.connectionState?.connected === true;
      });

    await page.notebook.activate(name);
    await page.locator(`${activePanel} .pret-view`).waitFor();
    await page.locator(`${activePanel} .pret-inline-fallback`).waitFor({
      state: "attached",
    });
    const restartBtn = page.locator(
      `${activePanel} .pret-restart-app-button`
    );
    await restartBtn.waitFor();
    await restartBtn.click();

    // Check that input checkbox "faire à manger" is present
    await page.notebook.activate(name);
    await page.locator(`${activePanel} .pret-view`).waitFor();
    const activeCheckbox = page.locator(`${activePanel} #faire-à-manger`);
    await activeCheckbox.waitFor({
      state: "attached",
    });
    await expect(activeCheckbox).toBeChecked();
    const desc = page.locator(`${activePanel} .pret-view p`);
    await desc.waitFor({
      state: "attached",
    });
    let expectedDescText = "Number of unfinished todo: 1";
    await expect(desc).toHaveText(expectedDescText);

    // Go offline
    await waitForPretConnection(true);
    await page.context().setOffline(true);
    await waitForPretConnection(false);

    const checkedCheckbox = page.locator(
      `${activePanel} #faire-à-manger:checked`
    );
    // The checkbox was checked before, it should stay checked when we're
    // offline and the user tries to interact with it
    await checkedCheckbox.waitFor({ state: "attached" });

    await expect(activeCheckbox).toBeChecked();

    // Go online
    await page.context().setOffline(false);
    await waitForPretConnection(true);

    // Uncheck the checkbox
    const uncheckedCheckbox = page.locator(
      `${activePanel} #faire-à-manger:not(:checked)`
    );
    await activeCheckbox.evaluate((element) => {
      (element as HTMLInputElement).click();
    });
    await uncheckedCheckbox.waitFor({ state: "attached" });
    await expect(activeCheckbox).not.toBeChecked();
  });
});
