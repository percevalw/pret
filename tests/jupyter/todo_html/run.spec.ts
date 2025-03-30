import { expect, test } from "@jupyterlab/galata";

test.describe("Notebook Tests", () => {
  test("TodoHTML", async ({ page, tmpPath }) => {
    const fileName = "todo_html/TodoHTML.ipynb";
    await page.notebook.openByPath(fileName);
    await page.waitForSelector(".jp-Notebook-ExecutionIndicator[data-status=idle]");
    await page.waitForTimeout(1000);
    await page.notebook.runCell(0, true);

    // Await for ".pret-view" element to appear
    await page.waitForSelector(".pret-view");

    // Check that input checkbox "faire à manger" is present
    const checkbox = await page.waitForSelector("#faire-à-manger");
    expect(checkbox).toBeTruthy();

    let desc = await page.waitForSelector(".pret-view p");
    let expectedDescText = "Number of unfinished todo: 1";
    expect(await desc.textContent()).toEqual(expectedDescText);

    // Uncheck the checkbox
    await checkbox.click();
    await page.waitForTimeout(1000);

    desc = await page.waitForSelector(".pret-view p");
    expectedDescText = "Number of unfinished todos: 2";
    expect(await desc.textContent()).toEqual(expectedDescText);

    // Check value from python kernel
    await page.notebook.addCell("code", "print(state['faire à manger'])");
    await page.notebook.runCell(1, true);
    await page.waitForTimeout(1000);
    const output = await page.waitForSelector(
      ".jp-Notebook > :nth-child(2) .jp-OutputArea-output pre"
    );

    expect(await output.textContent()).toEqual("False\n");

    // Edit value from python kernel
    await page.notebook.addCell("code", "state['faire à manger'] = True");
    await page.notebook.runCell(2, true);
    await page.waitForTimeout(1000);
    desc = await page.waitForSelector(".pret-view p");
    expectedDescText = "Number of unfinished todo: 1";
    expect(await desc.textContent()).toEqual(expectedDescText);

  });
});
