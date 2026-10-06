const { test, expect } = require(process.env.PRET_TESTING_HELPER);
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const esbuild = require("esbuild");

const runtimeFile = path.resolve(__dirname, "../../client/org.transcrypt.__runtime__.js");

// Load the Python factories inside each case so coverage records that case as their execution context
async function factories() {
  global.window = global;
  window.pret_modules = { "org.transcrypt.__runtime__": await import(pathToFileURL(runtimeFile).href) };
  const code = JSON.parse(fs.readFileSync(process.env.PRET_FIXTURE_BUNDLE)).code;
  const functions = new Function(code)();
  const get = (name) => functions[Object.keys(functions).find((key) => key.includes(`_${name}_`))]();
  expect(get("notebook_function")()).toBe(43);
  return get;
}

test("node", async () => {
  const get = await factories();
  expect(get("choose")(false)).toBe(-11);
  expect(get("choose_js")(false)).toBe(-21);
  expect(get("dynamic")()).toBe(31);
});

for (const [name, value, expected] of [
  ["positive", true, 21],
  ["negative", false, -21],
]) {
  test(name, async () => {
    const get = await factories();
    expect(get("choose_js")(value)).toBe(expected);
  });
}

test("failure", async () => {
  const get = await factories();
  expect(get("dynamic")()).toBe(31);
  throw new Error("Deliberate failure after measured execution");
});

test("browser", async ({ browser }) => {
  const bundle = JSON.parse(fs.readFileSync(process.env.PRET_FIXTURE_BUNDLE));
  const context = await browser.newContext();
  for (let attempt = 0; attempt < 2; attempt++) {
    const page = await context.newPage();
    await page.goto("about:blank");
    const runtime = esbuild.buildSync({
      entryPoints: [runtimeFile],
      bundle: true,
      write: false,
      format: "iife",
      globalName: "pyRuntime",
    }).outputFiles[0].text;
    await page.addScriptTag({ content: runtime });
    const typed = esbuild.buildSync({
      entryPoints: [path.join(__dirname, "sample.ts")],
      bundle: true,
      write: false,
      format: "iife",
      globalName: "tsFixture",
      sourcemap: "inline",
    }).outputFiles[0].text;
    await page.addScriptTag({ content: typed });
    expect(
      await page.evaluate((code) => {
        window.pret_modules = { "org.transcrypt.__runtime__": window.pyRuntime };
        const functions = new Function(code)();
        const get = (name) => functions[Object.keys(functions).find((key) => key.includes(`_${name}_`))]();
        if (get("notebook_function")() !== 43) throw new Error("Notebook function did not execute");
        return get("choose")(true) + get("choose_js")(true) + window.tsFixture.chooseTS(true);
      }, bundle.code)
    ).toBe(83);
    await page.reload();
    await page.close();
  }
  await context.close();
});
