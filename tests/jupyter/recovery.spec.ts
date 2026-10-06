import { writeFile } from "node:fs/promises";
import path from "node:path";
const { jupyterTest: test, expect, openNotebook } = require(process.env.PRET_TESTING_HELPER);
const panel = ".jp-NotebookPanel:not(.lm-mod-hidden)";
const cells = [
  "from pret import create_store\nfrom todo_app import TodoApp\n" +
    "state = create_store({'faire à manger': True, 'faire la vaisselle': False}, sync='todo.pret')\n" +
    "app = TodoApp(state=state)",
  "app",
];

// Keep real serialized outputs and stop their kernel so reopening goes through the restart of a saved view
async function saveStoppedNotebook(page, name) {
  await openNotebook(page, name, cells, { run: true });
  await expect(page.locator(`${panel} #faire-à-manger`)).toBeChecked();
  await page.notebook.save();
  await page.evaluate(async () => {
    const sessions = (window as any).jupyterapp.serviceManager.sessions;
    await sessions.shutdownAll();
  });
}

test("saved notebook restarts and preserves state while the kernel connection recovers", async ({ page }) => {
  const sockets = new Set<any>();
  let offline = false;
  await page.routeWebSocket(/api\/kernels\/.*\/channels/, (socket) => {
    if (offline) socket.close();
    else {
      socket.connectToServer();
      sockets.add(socket);
    }
  });
  await page.route(/\/api\/sessions(?:\?|$)/, (route) => (offline ? route.abort() : route.continue()));
  // Start recovery from the released ordinary Store file layout
  await writeFile(
    path.join(process.env.PRET_TEST_ROOT, "todo.pret"),
    Buffer.from(
      "MDRlNzFlOWItNGQ0NC00M2MzLWJiMGEtYWYwZmU1MjcyNjAxRwAAAAED6diFzA8AJwEBXwFfASgA6diFzA8A" +
        "D2ZhaXJlIMOgIG1hbmdlcgF4KADp2IXMDwASZmFpcmUgbGEgdmFpc3NlbGxlAXkA",
      "base64"
    )
  );
  await saveStoppedNotebook(page, "Recovery.ipynb");
  await page.reload();
  await page.locator(`${panel} .pret-inline-fallback`).waitFor({ state: "attached" });
  await page.locator(`${panel} .pret-restart-app-button`).click();
  const checkbox = page.locator(`${panel} #faire-à-manger`);
  const status = page.locator(`${panel} #connection-status`);
  await expect(checkbox).toBeChecked();
  await expect(page.locator(`${panel} .pret-view p`)).toHaveText("Number of unfinished todo: 1");
  await expect(status).toHaveText("online");
  offline = true;
  await page.context().setOffline(true);
  for (const socket of sockets) socket.close();
  await page.evaluate(() => (window as any).jupyterapp.serviceManager.sessions.refreshRunning().catch(() => {}));
  await expect(status).toHaveText("offline", { timeout: 30000 });
  await expect(checkbox).toBeChecked();
  offline = false;
  await page.context().setOffline(false);
  // Allow the Jupyter socket retry to finish after the offline interval
  await expect(status).toHaveText("online", { timeout: 60000 });
  await checkbox.evaluate((element) => (element as HTMLInputElement).click());
  await expect(checkbox).not.toBeChecked();
});

test("full page saved view restarts and fills the viewport", async ({ page }) => {
  await saveStoppedNotebook(page, "FullPage.ipynb");
  const url = new URL("/doc/tree/FullPage.ipynb?pret-fullpage-cell=1", page.url()).toString();
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await page.page.goto(url, { waitUntil: "domcontentloaded" });
  const host = page.locator("#pret-fullpage-host");
  await expect(host).toBeVisible();
  await expect(page.locator("body")).toHaveClass(/pret-fullpage/);
  await host.locator(".pret-restart-app-button").click();
  await expect(page.locator("#main")).toBeHidden();
  await expect(page.locator("#todoapp")).toBeVisible();
  expect(await host.screenshot()).toEqual(await page.screenshot());
});
