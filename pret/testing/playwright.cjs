const fs = require("node:fs/promises");
const path = require("node:path");
const { createRequire } = require("node:module");
const projectRequire = createRequire(path.join(process.cwd(), "package.json"));
const playwright = projectRequire("@playwright/test");
const coverage = require("./coverage.cjs");

// Start before scenario imports so Node coverage includes application module initialization
coverage.startNode();

// Use the same browser and Node checkpoints for client tests and Galata notebook tests
function instrument(base) {
  return base.extend({
    browser: [
      async ({ browser }, use) => {
        coverage.watchBrowser(browser);
        await use(browser);
      },
      { scope: "worker" },
    ],
    capture: [
      async ({}, use) => {
        await coverage.nodeCheckpoint(`${process.env.PRET_COVERAGE_TEST}::<setup>`);
        try {
          await use();
        } finally {
          await coverage.nodeCheckpoint(process.env.PRET_COVERAGE_TEST);
        }
      },
      { auto: true },
    ],
  });
}

const test = instrument(playwright.test);
const jupyterLab3 = process.env.JUPYTERLAB_VERSION?.startsWith("3.");
const galata = projectRequire(jupyterLab3 ? "old-galata" : "@jupyterlab/galata");
const realServer = { mockState: false, mockSettings: false, mockConfig: false, mockUser: false };

// Restored workspaces can have a notebook active instead of the Launcher expected by Galata
async function waitForApplication(page) {
  await page.waitForFunction(() => Boolean(window.jupyterapp));
  await page.evaluate(() => window.jupyterapp.restored);
}

const jupyterTest = instrument(galata.test).extend({
  waitForApplication: async ({}, use) => use(waitForApplication),
  serverFiles: "on",
  ...realServer,
  kernels: null,
  sessions: null,
  terminals: null,
});

// Give a peer window its own saved workspace while sharing the real server and coverage hooks
async function newJupyterPage(browser, tmpPath, workspace) {
  const appPath = `/lab/workspaces/${encodeURIComponent(workspace)}`;
  // Galata for JupyterLab 3 takes positional arguments and returns the page directly
  if (jupyterLab3) {
    return galata.galata.newPage(
      appPath,
      true,
      process.env.PRET_TEST_URL,
      browser,
      false,
      false,
      false,
      null,
      null,
      tmpPath,
      waitForApplication
    );
  }
  const { page } = await galata.galata.newPage({
    browser,
    tmpPath,
    waitForApplication,
    ...realServer,
    baseURL: process.env.PRET_TEST_URL,
    appPath,
    mockKernels: false,
    mockSessions: false,
    mockTerminals: false,
  });
  return page;
}

// Generate setup notebooks in the temporary server root used by this individual case
async function writeNotebook(name, cells) {
  const destination = path.join(process.env.PRET_TEST_ROOT, name);
  await fs.mkdir(path.dirname(destination), { recursive: true });
  await fs.writeFile(
    destination,
    JSON.stringify({
      nbformat: 4,
      nbformat_minor: 5,
      metadata: { kernelspec: { name: "python3", display_name: "Python 3", language: "python" } },
      cells: cells.map((source, index) => ({
        id: `cell-${index}`,
        cell_type: "code",
        source,
        metadata: {},
        outputs: [],
        execution_count: null,
      })),
    })
  );
}

// Open retained or generated notebooks through the real document command without file browser navigation races
async function openNotebook(page, name, cells, { run = false } = {}) {
  if (cells) await writeNotebook(name, cells);
  await page.evaluate(async (name) => {
    const app = window.jupyterapp;
    const notebook = await app.commands.execute("docmanager:open", { path: name, factory: "Notebook" });
    await notebook.context.ready;
    // Running a cell before the session attaches its kernel would open the kernel selection dialog
    await notebook.sessionContext.ready;
    app.shell.activateById(notebook.id);
  }, name);
  await page.waitForSelector(".jp-Notebook-ExecutionIndicator[data-status=idle]");
  if (run) await page.notebook.run();
}

// Return a live shell widget handle for notebook and viewer state checks across JupyterLab versions
async function getMainWidget(page, id) {
  return page.evaluateHandle((id) => {
    const widgets = window.jupyterapp.shell.widgets("main");
    let widget;
    while ((widget = widgets.next()) && !widget.done) {
      widget = widget.value ?? widget;
      if (widget.id === id) return widget;
    }
    throw new Error(`Main widget not found, ${id}`);
  }, id);
}

// Append code used by notebook scenarios to inspect or mutate their Python application, the editor is filled
// through its textbox for both supported JupyterLab versions
async function addCodeCell(page, activePanel, source) {
  const index = await page.notebook.getCellCount();
  await page.notebook.addCell("code", "");
  await page.notebook.selectCells(index);
  await page.notebook.enterCellEditingMode(index);
  await page
    .locator(`${activePanel} .jp-Cell:nth-child(${index + 1})`)
    .getByRole("textbox")
    .fill(source);
  await page.notebook.leaveCellEditingMode(index);
  return index;
}

module.exports = {
  test,
  jupyterTest,
  expect: playwright.expect,
  newJupyterPage,
  writeNotebook,
  openNotebook,
  addCodeCell,
  getMainWidget,
};
