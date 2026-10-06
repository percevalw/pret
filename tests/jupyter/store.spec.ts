const { jupyterTest: test, expect, openNotebook, addCodeCell, newJupyterPage } = require(process.env
  .PRET_TESTING_HELPER);
const panel = ".jp-NotebookPanel:not(.lm-mod-hidden)";

// Open the actual PRET component so resources and callbacks use Jupyter comms
async function openStore(page, mode = "full", name = "Store.ipynb", file = "True", maxBytes = 1000000) {
  await openNotebook(
    page,
    name,
    [`from store_app import setup, StoreApp\nstate = setup('${mode}', ${file}, ${maxBytes})\nStoreApp(store=state)`],
    { run: true }
  );
  await expect(page.locator(`${panel} #store-app`)).toBeVisible();
  await page.evaluate(async () => {
    const w = window as any;
    await w.storeLib.getStoreHistory(w.fixtureStore).ready;
    await Promise.all([w.fixtureStore.todos.ensureScope(), w.fixtureStore.tags.ensureScope()]);
  });
}

for (const mode of ["paged", "full"]) {
  test(`${mode} Store sends mixed edits and awaited callbacks through notebook history`, async ({ page }) => {
    await openStore(page, mode);
    await page.evaluate(async (mode) => {
      const w = window as any;
      const state = w.fixtureStore;
      const lib = w.storeLib;
      const history = lib.getStoreHistory(state);
      const row = async () => state.todos.decodeRows((await state.todos.query({ identities: [1] })).bytes)[0];
      const check = (condition, message) => {
        if (!condition) throw new Error(message);
      };
      const settle = async (revision = 0) => {
        for (let attempt = 0; attempt < 100; attempt++) {
          const current = history.getSnapshot();
          if (!current.pending && current.revision >= revision) return;
          await new Promise((resolve) => setTimeout(resolve, 50));
        }
        throw new Error("Store action did not reach its accepted revision");
      };
      state.todo.milk = true;
      let guarded = false;
      try {
        state.todo.milk = "overlapping";
      } catch {
        guarded = true;
      }
      check(guarded && state.todo.milk, "Only one local action awaits acceptance");
      await settle();
      const [, endNested] = lib.beginTransaction(w.fixtureTodo);
      state.todo.milk = "shared";
      check(w.fixtureTodo.milk === "shared", "Nested values sent on their own see edits of their transaction");
      endNested();
      await settle();
      check(w.fixtureTodo.milk === "shared", "Nested values sent on their own keep edits of their transaction");
      state.todo.milk = true;
      await settle();
      await state.todos.apply_edit({ row_identity: 1, values: { label: "michael", score: 2 } });
      await settle(await w.fixturePeer({ label: "michael" }));
      await history.undo();
      check(
        (await row()).label === "michael" && (await row()).score === 0,
        "Selective undo preserves an equal value peer rewrite"
      );
      await history.redo();
      check((await row()).score === 2, "Redo restores only inverse cells");
      await settle(await w.fixtureCallback("michael"));
      check(state.todo.report && (await row()).score === 3, "Awaited callback commits ordinary and table state");
      await history.undo();
      check(!state.todo.report && (await row()).score === 2, "Callback joins the initiating page history");
      const revision = history.getSnapshot().revision;
      const [, end] = lib.beginTransaction(state);
      state.todo.report = true;
      const edit = state.todos.apply_edit({ row_identity: 1, values: { score: 4 } });
      end();
      await edit;
      await settle();
      check(
        history.getSnapshot().revision === revision + 1 && state.todo.report && (await row()).score === 4,
        "Explicit mixed transaction commits one revision"
      );
      await history.undo();
      check(!state.todo.report && (await row()).score === 2, "Mixed undo selects the same action");
      state.todo.report = true;
      await settle();
      // A paged Store rejects the invalid table edit after sending it, a full Store throws while applying it
      const acceptedRevision = history.getSnapshot().revision;
      const [, rejectEnd] = lib.beginTransaction(state);
      state.todo.milk = "rejected";
      let invalid;
      let error;
      try {
        invalid = state.todos.apply_edit({ row_identity: 1, values: { score: "invalid" } });
      } catch (caught) {
        error = caught;
      }
      rejectEnd(error?.name, error);
      await invalid;
      await settle();
      check(
        (mode === "full") === Boolean(error) &&
          state.todo.milk === true &&
          (await row()).score === 2 &&
          history.getSnapshot().revision === acceptedRevision,
        "Invalid mixed edit restores both state types without committing"
      );
      await history.undo();
      check(!state.todo.report, "Earlier ordinary action remains undoable");
      await history.redo();
      check(state.todo.report, "Earlier ordinary action remains redoable");
      await settle();
      await w.fixtureFailCommit();
      state.todo.milk = "failed";
      await settle();
      check(state.todo.milk === true && state.todo.report, "A failed commit keeps accepted ordinary actions");
      check(history.canUndo && history.getSnapshot().error, "A failed commit keeps the page history");
      await history.undo();
      check(!state.todo.report, "The action before a failed commit is still undoable");
      await history.redo();
    }, mode);
    await addCodeCell(
      page,
      panel,
      "assert state['todo']['milk'] is True\nassert state['todo']['report'] is True\nprint('accepted')"
    );
    await page.notebook.runCell(1, true);
    await expect(page.locator(`${panel} .jp-Cell:nth-child(2) .jp-OutputArea-output pre`)).toHaveText("accepted\n");
  });
}

test("file backed Store pages keep peer edits and clear history on reload", async ({ page, browser, tmpPath }) => {
  const peer = await newJupyterPage(browser, tmpPath, "peer");
  try {
    await openStore(page, "full", "Michael.ipynb", "'shared.pret'");
    await openStore(peer, "full", "Dwight.ipynb", "'shared.pret'");
    await page.evaluate(() => {
      (window as any).fixtureStore.todo.milk = true;
    });
    await expect.poll(() => peer.evaluate(() => (window as any).fixtureStore.todo.milk)).toBe(true);
    expect(
      await peer.evaluate(() => {
        const w = window as any;
        return w.storeLib.getStoreHistory(w.fixtureStore).canUndo;
      })
    ).toBe(false);
    await peer.evaluate(() => {
      (window as any).fixtureStore.todo.report = true;
    });
    await expect.poll(() => page.evaluate(() => (window as any).fixtureStore.todo.report)).toBe(true);
    await page.evaluate(async () => {
      const w = window as any;
      await w.storeLib.getStoreHistory(w.fixtureStore).undo();
    });
    await expect
      .poll(() =>
        peer.evaluate(() => {
          const state = (window as any).fixtureStore;
          return [state.todo.milk, state.todo.report];
        })
      )
      .toEqual([false, true]);
    await peer.evaluate(async () => {
      const w = window as any;
      await w.storeLib.getStoreHistory(w.fixtureStore).undo();
    });
    await expect.poll(() => page.evaluate(() => (window as any).fixtureStore.todo.report)).toBe(false);
    await peer.evaluate(async () => {
      const w = window as any;
      await w.storeLib.getStoreHistory(w.fixtureStore).redo();
    });
    await page.evaluate(async () => {
      const w = window as any;
      await w.storeLib.getStoreHistory(w.fixtureStore).redo();
    });
    await expect
      .poll(() =>
        peer.evaluate(() => {
          const state = (window as any).fixtureStore;
          return [state.todo.milk, state.todo.report];
        })
      )
      .toEqual([true, true]);
    await addCodeCell(page, panel, "state['todo']['milk'] = False\nstate['todo']['report'] = False");
    await page.notebook.runCell(1, true);
    await expect(page.locator(`${panel} #milk`)).toHaveText("no milk");
    await expect(peer.locator(`${panel} #milk`)).toHaveText("no milk");
    await expect(peer.locator(`${panel} #report`)).toHaveText("no report");
    await addCodeCell(page, panel, "state['todo']['milk'] = True\nstate['todo']['report'] = True");
    await page.notebook.runCell(2, true);
    await expect(peer.locator(`${panel} #milk`)).toHaveText("milk");
    await expect(peer.locator(`${panel} #report`)).toHaveText("report");
    await page.evaluate(async () => {
      await (window as any).fixtureStore.todos.apply_edit({ row_identity: 1, values: { label: "collaboration" } });
    });
    await expect
      .poll(() =>
        peer.evaluate(async () => {
          const w = window as any;
          return w.fixtureStore.todos.decodeRows((await w.fixtureStore.todos.query({ identities: [1] })).bytes)[0]
            .label;
        })
      )
      .toBe("collaboration");
    await peer.evaluate(async () => {
      await (window as any).fixtureStore.todos.apply_edit({ row_identity: 1, values: { label: "collaboration" } });
    });
    await page.evaluate(async () => {
      const w = window as any;
      await w.storeLib.getStoreHistory(w.fixtureStore).undo();
    });
    await expect
      .poll(() =>
        page.evaluate(async () => {
          const w = window as any;
          return w.fixtureStore.todos.decodeRows((await w.fixtureStore.todos.query({ identities: [1] })).bytes)[0]
            .label;
        })
      )
      .toBe("collaboration");
    // Both pages push to one list from the same revision, Python keeps both items in commit order
    await page.evaluate(() => {
      const w = window as any;
      w.capturedItem = w.fixtureStore.items[0];
    });
    await Promise.all([
      page.evaluate(() => (window as any).fixtureStore.items.push({ text: "michael" })),
      peer.evaluate(() => (window as any).fixtureStore.items.splice(0, 0, { text: "dwight" })),
    ]);
    const texts = () => (window as any).storeLib.snapshot((window as any).fixtureStore.items).map((item) => item.text);
    await expect.poll(() => page.evaluate(texts)).toEqual(["dwight", "first", "michael"]);
    await expect.poll(() => peer.evaluate(texts)).toEqual(["dwight", "first", "michael"]);
    // A value captured before the peer insertion keeps following its item
    await page.evaluate(() => {
      (window as any).capturedItem.done = true;
    });
    await expect.poll(() => peer.evaluate(() => (window as any).fixtureStore.items[1].done)).toBe(true);
    await peer.notebook.save();
    await peer.reload();
    await peer.locator(`${panel} #store-app`).waitFor();
    await expect
      .poll(() =>
        peer.evaluate(() => {
          const w = window as any;
          const history = w.storeLib.getStoreHistory(w.fixtureStore);
          return [w.fixtureStore.todo.milk, w.fixtureStore.todo.report, history.canUndo, history.canRedo];
        })
      )
      .toEqual([true, true, false, false]);
  } finally {
    await peer.context().close();
  }
});

for (const mode of ["paged", "full"]) {
  test(`${mode} Store optimistic caches handle delayed replies rejection and lost acknowledgements`, async ({
    page,
  }) => {
    await openStore(page, mode);
    const { loadClient } = await import("../client/browser");
    await loadClient(page, ['export {optimisticEdits} from "./tests/jupyter/store.browser";']);
    await page.evaluate(async (mode) => {
      const { optimisticEdits } = await import("/pret-client.mjs");
      return optimisticEdits(mode);
    }, mode);
  });
}

test("complete Store copies show temporary identities row deltas and stale revision conflicts", async ({ page }) => {
  await openStore(page);
  await page.evaluate(async () => {
    const w = window as any;
    const state = w.fixtureStore;
    const history = w.storeLib.getStoreHistory(state);
    const rows = async (frame, query = {}) => frame.decodeRows((await frame.query(query)).bytes);
    const check = (condition, message) => {
      if (!condition) throw new Error(message);
    };
    const commit = history.commitRemoteOp;
    let release;
    history.commitRemoteOp = async (action) => {
      await new Promise((resolve) => {
        release = resolve;
      });
      return commit(action);
    };
    const insertion = state.tags.apply_edit({
      kind: "insert",
      values: { id: "new", todo_id: "todo", label: "added" },
    });
    const temporary = (await rows(state.tags, { sort: [{ column: "label", direction: "asc" }] }))[0];
    check(
      temporary.id === "new" && temporary.__pret_row_id < 0 && history.getSnapshot().pending,
      "Insertions show temporary identities while pending"
    );
    release();
    await insertion;
    const identity = state.tags.resolve_identity(temporary.__pret_row_id);
    check(
      identity > 0 && (await rows(state.tags, { identities: [identity] }))[0].id === "new",
      "Acceptance maps temporary identities"
    );
    history.commitRemoteOp = commit;
    const initial = await w.fixtureBackendData();
    const deletion = state.tags.apply_edit({ kind: "delete", row_identity: identity });
    check((await rows(state.tags)).length === 1, "Deletion is immediately visible");
    await deletion;
    const delta = await w.fixtureBackendData(initial.revision);
    check(
      delta.tables.length === 0 && delta.commits.length === 1 && delta.commits[0].changes[0].kind === "delete",
      "Deletion transfers only committed row changes"
    );
    await history.undo();
    check((await rows(state.tags)).length === 2, "Connected undo restores the row");
    await history.redo();
    check((await rows(state.tags)).length === 1, "Connected redo removes the restored row");
    const stale = history.getSnapshot().revision;
    const revision = await w.fixturePeer({ label: "peer" });
    for (let attempt = 0; attempt < 100 && history.getSnapshot().revision < revision; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    const conflict = await state.todos.apply_edit({
      base_revision: stale,
      row_identity: 1,
      values: { label: "stale" },
    });
    check(
      conflict.status === "conflict" && (await rows(state.todos, { identities: [1] }))[0].label === "peer",
      "A displayed stale revision protects a peer edit after catch up"
    );
    const addedRevision = await w.fixtureAddTable();
    for (let attempt = 0; attempt < 100 && history.getSnapshot().revision < addedRevision; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    check(
      state.todos.decodeRows((await state.added.query({})).bytes)[0].label === "new table",
      "New materializations arrive with their ordinary reference"
    );
  });
});

test("complete Store copies reject the byte limit without a paged fallback", async ({ page }) => {
  await openNotebook(
    page,
    "TooLarge.ipynb",
    ["from store_app import setup, StoreApp\nstate = setup('full', True, 1)\nStoreApp(store=state)"],
    { run: true }
  );
  await expect(page.locator(`${panel} .pret-view`)).toContainText("download exceeds dataframe_max_bytes of 1");
  await expect(page.locator(`${panel} #store-app`)).toHaveCount(0);
});
