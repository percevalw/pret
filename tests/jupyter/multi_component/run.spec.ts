let { expect, test } = require("@jupyterlab/galata");

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
    }
    catch (e) {
      console.error("Could not create screenshot");
    }
  }
});

test.describe("Notebook Tests", () => {
  test("MultiComponent", async ({ page, tmpPath }) => {
    const fileName = "multi_component/MultiComponent.ipynb";
    await page.notebook.openByPath(fileName);
    await page.waitForSelector(
      ".jp-Notebook-ExecutionIndicator[data-status=idle]"
    );
    await page.waitForTimeout(1000);
    await page.notebook.runCellByCell();

    // Await for ".pret-view" element to appear
    await page.waitForSelector(".pret-view");

    // Check that input checkbox "faire à manger" is present
    const checkbox = await page.waitForSelector("#todo-checkbox-1", {
      state: "attached",
    });
    expect(await checkbox.inputValue()).toBeFalsy();

    let desc = await page.waitForSelector("#remaining-todo-counter", {
      state: "attached",
    });
    let expectedDescText = "Number of unfinished todos: 1.";
    expect(await desc.textContent()).toEqual(expectedDescText);

    // Uncheck the checkbox
    await checkbox.click();
    await page.waitForTimeout(1000);

    desc = await page.waitForSelector("#remaining-todo-counter", { state: "attached" });
    expectedDescText = "Number of unfinished todos: 0.";
    expect(await desc.textContent()).toEqual(expectedDescText);
  });
});
