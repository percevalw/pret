let { expect, test } = require("@jupyterlab/galata");
const path = require("path");
const fs = require("fs");

// JUPYTERLAB_VERSION is set in run.sh
if (process.env.JUPYTERLAB_VERSION < "4") {
  console.log("Using old galata to match JupyterLab 3");
  const oldGalata = require("old-galata");
  expect = oldGalata.expect;
  test = oldGalata.test;
}

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
});

test.describe("Notebook Tests", () => {
  test("TodoHTML Synchronization", async ({ page, tmpPath }) => {
    const syncFile = `${tmpPath}/sync_file.bin`;
    try {
      fs.unlinkSync(`tests/jupyter/file_sync/${syncFile}`);
    } catch (e) {
      // ignore if missing
    }
    const activePanel = ".jp-NotebookPanel:not(.lm-mod-hidden)";
    const fileName = "file_sync/TodoHTML.ipynb";
    const name = path.basename(fileName);
    await page.notebook.openByPath(fileName);
    await page.waitForTimeout(1000);

    const fileNameBis = "file_sync/TodoHTML2.ipynb";
    const nameBis = path.basename(fileNameBis);
    await page.notebook.openByPath(fileNameBis);
    await page.waitForTimeout(1000);

    await page.notebook.activate(name);
    await page.waitForTimeout(1000);
    await page.notebook.setCell(
      0,
      "code",
      "import os\n" +
        `os.environ['SYNC_FILE'] = '${syncFile}'\n` +
        "from todoapp import TodoApp, state  # noqa\n" +
        "TodoApp()"
    );
    await page.notebook.runCell(0, true);
    await page.waitForSelector(`${activePanel} .pret-view`);

    await page.notebook.activate(nameBis);
    await page.waitForTimeout(1000);
    await page.notebook.setCell(
      0,
      "code",
      "import os\n" +
        `os.environ['SYNC_FILE'] = '${syncFile}'\n` +
        "from todoapp import TodoApp, state  # noqa\n" +
        "TodoApp()"
    );
    await page.notebook.runCell(0, true);
    await page.waitForSelector(`${activePanel} .pret-view`);

    // Check that input checkbox "faire à manger" is present
    await page.notebook.activate(name);
    const checkbox = await page.waitForSelector(
      `${activePanel} #faire-à-manger`,
      {
        state: "attached",
      }
    );
    expect(checkbox).toBeTruthy();

    let desc = await page.waitForSelector(`${activePanel} .pret-view p`, {
      state: "attached",
    });
    let expectedDescText = "Number of unfinished todo: 1";
    expect(await desc.textContent()).toEqual(expectedDescText);

    // Uncheck the checkbox
    await checkbox.click();
    await page.waitForTimeout(1000);

    desc = await page.waitForSelector(`${activePanel} .pret-view p`, {
      state: "attached",
    });
    expectedDescText = "Number of unfinished todos: 2";
    expect(await desc.textContent()).toEqual(expectedDescText);

    // Check value from python kernel
    await page.notebook.addCell("code", "print(state['faire à manger'])");
    await page.notebook.runCell(1, true);
    const output = await page.waitForSelector(
      ".jp-Notebook > :nth-child(2) .jp-OutputArea-output pre",
      { state: "attached" }
    );

    expect(await output.textContent()).toEqual("False\n");

    // Edit value from python kernel
    await page.notebook.addCell(
      "code",
      "state['faire la vaisselle'] = True\n" + "state['faire à manger'] = True"
    );
    await page.notebook.runCell(2, true);
    desc = await page.waitForSelector(`${activePanel} .pret-view p`, {
      state: "attached",
    });
    expectedDescText = "Number of unfinished todo: 0";
    expect(await desc.textContent()).toEqual(expectedDescText);
    await page.waitForTimeout(1000);

    await page.notebook.activate(nameBis);
    desc = await page.waitForSelector(`${activePanel} .pret-view p`, {
      state: "attached",
    });
    expectedDescText = "Number of unfinished todo: 0";
    expect(await desc.textContent()).toEqual(expectedDescText);
  });
});
