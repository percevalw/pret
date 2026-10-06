const fs = require("node:fs");
const path = require("node:path");
const inspector = require("node:inspector");
const { promisify } = require("node:util");

const directory = process.env.PRET_COVERAGE_DIR;
const testId = process.env.PRET_COVERAGE_TEST;
let sequence = 0;
let session, post;
const browsers = new WeakSet();
const contexts = new WeakSet();
const pages = new Map();

// Save collection failures separately so the report command cannot silently publish incomplete coverage
function failure(error) {
  fs.appendFileSync(
    path.join(directory, "errors.jsonl"),
    JSON.stringify({ test: testId, error: String(error) }) + "\n"
  );
}

// Keep V8 ranges and source together because temporary builds and VM scripts disappear after the test
function save(runtime, test, entries) {
  const filtered = entries.filter(
    (entry) =>
      !entry.url.startsWith("node:") &&
      (entry.url.startsWith("pret-coverage:") ||
        entry.source?.includes("sourceMappingURL=") ||
        /\/(?:pret|metanno)\/static\//.test(entry.url) ||
        (entry.url.startsWith(process.cwd() + path.sep) && !entry.url.includes("/node_modules/")))
  );
  fs.mkdirSync(path.join(directory, "raw"), { recursive: true });
  fs.writeFileSync(
    path.join(directory, "raw", `${process.pid}-${sequence++}.json`),
    JSON.stringify({ runtime, test, entries: filtered })
  );
}

// Collect before navigation destroys the old document and before closing its browser target
async function checkpoint(page, restart) {
  const state = pages.get(page);
  if (!state?.active) return;
  state.active = false;
  try {
    save("browser", testId, await page.coverage.stopJSCoverage());
    if (restart) {
      await page.coverage.startJSCoverage({ resetOnNavigation: false, reportAnonymousScripts: true });
      state.active = true;
    }
  } catch (error) {
    failure(error);
  }
}

// Install page checkpoints before returning newly created pages to the test or Galata
function watchPage(page) {
  if (pages.has(page)) return pages.get(page).ready;
  const state = { active: false, ready: null };
  pages.set(page, state);
  state.ready = (async () => {
    await page.coverage.startJSCoverage({ resetOnNavigation: false, reportAnonymousScripts: true });
    state.active = true;
    for (const name of ["goto", "reload", "goBack", "goForward", "close"]) {
      const original = page[name].bind(page);
      page[name] = async (...args) => {
        await checkpoint(page, name !== "close");
        return original(...args);
      };
    }
    page.on("close", () => {
      if (state.active) failure("Browser page closed before its coverage checkpoint");
      pages.delete(page);
    });
    page.on("crash", () => failure("Browser page crashed during coverage collection"));
  })();
  return state.ready;
}

// Instrument each context once including contexts made by browser newPage
function watchContext(context) {
  if (contexts.has(context)) return;
  contexts.add(context);
  context.on("page", (page) => watchPage(page).catch(failure));
  const newPage = context.newPage.bind(context);
  context.newPage = async (...args) => {
    const page = await newPage(...args);
    await watchPage(page);
    return page;
  };
  const close = context.close.bind(context);
  context.close = async (...args) => {
    for (const page of context.pages()) await checkpoint(page, false);
    return close(...args);
  };
}

// Share the same collector between Galata and standalone Playwright runners
function watchBrowser(browser) {
  if (!directory || browsers.has(browser)) return browser;
  if (browser.browserType().name() !== "chromium") throw new Error("Coverage requires Chromium");
  browsers.add(browser);
  const newContext = browser.newContext.bind(browser);
  browser.newContext = async (...args) => {
    const context = await newContext(...args);
    watchContext(context);
    return context;
  };
  const newPage = browser.newPage.bind(browser);
  browser.newPage = async (...args) => {
    const page = await newPage(...args);
    watchContext(page.context());
    await watchPage(page);
    return page;
  };
  const close = browser.close.bind(browser);
  browser.close = async (...args) => {
    for (const context of browser.contexts()) {
      for (const page of context.pages()) await checkpoint(page, false);
    }
    return close(...args);
  };
  return browser;
}

// Start the inspector synchronously so module initialization is measured before Node test hooks run
function startNode() {
  if (!directory || session) return;
  session = new inspector.Session();
  session.connect();
  post = promisify(session.post.bind(session));
  session.post("Debugger.enable");
  session.post("Profiler.enable");
  session.post("Profiler.startPreciseCoverage", { callCount: true, detailed: true });
}

// Resolve VM source before its context or temporary files are discarded
async function nodeCheckpoint(test) {
  if (!directory) return;
  const { result } = await post("Profiler.takePreciseCoverage");
  const entries = [];
  for (const entry of result) {
    if (entry.url.startsWith("node:") || entry.url.includes("/node_modules/")) continue;
    const { scriptSource } = await post("Debugger.getScriptSource", { scriptId: entry.scriptId });
    entries.push({ ...entry, source: scriptSource });
  }
  save("node", test, entries);
}

module.exports = { watchBrowser, startNode, nodeCheckpoint };
