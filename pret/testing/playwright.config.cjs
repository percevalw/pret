// Playwright configuration passed by the runner with --config, tests are found from the project directory where
// the runner starts, eg tests/client and tests/jupyter of Pret or Metanno
const path = require("node:path");
const { createRequire } = require("node:module");
const { devices } = createRequire(path.join(process.cwd(), "package.json"))("@playwright/test");

module.exports = {
  testDir: process.cwd(),
  testMatch: "**/*.spec.ts",
  workers: 1,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  reporter: "list",
  use: {
    baseURL: process.env.PRET_TEST_URL,
    viewport: { width: 1280, height: 900 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "firefox", use: { ...devices["Desktop Firefox"] } },
    { name: "webkit", use: { ...devices["Desktop Safari"] } },
  ],
};
