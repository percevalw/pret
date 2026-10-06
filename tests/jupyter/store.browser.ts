// Hold real notebook comm operations to test optimistic caches and uncertain acknowledgements
export async function optimisticEdits(mode: "paged" | "full") {
  const w = window as any;
  const frame = w.fixtureStore.todos;
  const history = w.storeLib.getStoreHistory(w.fixtureStore);
  const commit = history.commitRemoteOp;
  const synchronize = history.getRemoteDataChanges;
  let queryCalls = 0;
  let reject = false;
  let hold = true;
  let loseReply = false;
  let lastPayload: any;
  let uncertainId: string;
  let uncertainRevision: number;
  const attempts = [];
  let delayQuery = false;
  let releaseRows: () => void;
  let release: () => void;
  history.commitRemoteOp = async (action) => {
    if (hold)
      await new Promise<void>((resolve) => {
        release = resolve;
      });
    if (reject) await w.fixtureFailCommit();
    attempts.push(action.request_id);
    const result = await commit(action);
    if (loseReply) {
      loseReply = false;
      uncertainId = action.request_id;
      uncertainRevision = result.revision;
      throw new Error("Lost acknowledgement");
    }
    return result;
  };
  history.getRemoteDataChanges = async (revision, watched) => {
    const payload = await synchronize(revision, watched);
    lastPayload = payload;
    if (mode === "paged" && payload.tables.length) throw new Error("Paged synchronization downloaded a table");
    return payload;
  };
  const check = (condition, message) => {
    if (!condition) throw new Error(message);
  };
  try {
    await history.ready;
    const query = frame.query;
    frame.query = async (options) => {
      queryCalls++;
      const result = await query(options);
      if (delayQuery) {
        delayQuery = false;
        await new Promise<void>((resolve) => {
          releaseRows = resolve;
        });
      }
      return result;
    };
    await frame.ensureScope();
    const first = await frame.createView(null, "null", "id");
    const second = await frame.createView(null, "null", "id");
    const signal = new AbortController().signal;
    await first.getRows(0, 1, signal);
    await second.getRowsByKeys(["todo"], signal);
    let notifications = 0;
    first.subscribeRows(() => {
      notifications++;
    });
    let calls = queryCalls;
    const pending = frame.applyEdit(first.getCachedRow(0), "label", "urgent");
    check(first.getCachedRow(0).label === "urgent", "Table must immediately display its pending value");
    check(
      (await second.getRowsByKeys(["todo"], signal))[0].label === "urgent",
      "Identity readers share the pending value"
    );
    check(frame.loading === null && !first.disposed, "A cell edit must keep the query source available");
    const additional = await frame.createView(null, "null", "id");
    check(
      (await additional.getRows(0, 1, signal))[0].label === "urgent",
      "Rows fetched while saving include the pending value"
    );
    calls = queryCalls;
    release();
    await pending;
    check(first.getCachedRow(0).label === "urgent", "Acceptance must preserve the displayed value");
    check(notifications === 1, "Matching acceptance must not publish another row change");
    check(queryCalls === calls, "Matching acceptance must not query rows or counts");
    check(!first.disposed && !second.disposed, "Unrelated views must keep their row sources");
    const delayed = await frame.createView(null, "null", "id");
    delayQuery = true;
    const reading = delayed.getRows(0, 1, signal);
    for (let attempt = 0; attempt < 100 && !releaseRows; attempt++)
      await new Promise((resolve) => setTimeout(resolve, 10));
    check(Boolean(releaseRows), "The page response is held before publication");
    const newer = frame.applyEdit(first.getCachedRow(0), "label", "fresh");
    release();
    await newer;
    releaseRows();
    check((await reading)[0].label === "fresh", "A delayed page preserves the accepted cell value");

    reject = true;
    const rejected = frame.applyEdit(first.getCachedRow(0), "label", "rejected");
    check(first.getCachedRow(0).label === "rejected", "Rejected edits are initially optimistic");
    release();
    await rejected;
    check(first.getCachedRow(0).label === "fresh", "Rejection restores the accepted value");
    check(Boolean(history.getSnapshot().error), "Rejection exposes the save error");
    reject = false;
    const concurrent = frame.applyEdit(first.getCachedRow(0), "label", "local");
    await w.fixturePeer({ comment: "peer comment" });
    release();
    await concurrent;
    check(
      first.getCachedRow(0).label === "local" && first.getCachedRow(0).comment === "peer comment",
      "Acceptance preserves a concurrent change to another cell"
    );
    const conflict = frame.applyEdit(first.getCachedRow(0), "label", "conflicting");
    await w.fixturePeer({ label: "peer label" });
    release();
    await conflict;
    check(first.getCachedRow(0).label === "peer label", "Rejection preserves the authoritative concurrent value");
    loseReply = true;
    const uncertain = frame.applyEdit(first.getCachedRow(0), "label", "urgent");
    release();
    await uncertain;
    check(
      first.getCachedRow(0).label === "urgent" && history.getSnapshot().pending,
      "Transport uncertainty preserves the pending value"
    );
    hold = false;
    history.setConnected(true);
    for (let attempt = 0; attempt < 100 && history.getSnapshot().pending; attempt++)
      await new Promise((resolve) => setTimeout(resolve, 10));
    check(
      !history.getSnapshot().pending && first.getCachedRow(0).label === "urgent",
      "Retry resolves the same operation without replacing its value"
    );
    check(attempts.filter((id) => id === uncertainId).length === 2, "Retry reuses the same operation identifier");
    check(
      (await w.fixtureBackendData()).revision === uncertainRevision,
      "Lost acknowledgement retry does not commit another revision"
    );
    hold = true;
    const revision = frame.revision;
    const batch = frame.applyMutation({
      kind: "batch",
      edits: [
        { row_identity: 1, values: { comment: "batch" } },
        { row_identity: 2, values: { comment: "batch" } },
      ],
    });
    check(
      first.getCachedRow(0).comment === "batch" && first.getCachedRow(1).comment === "batch",
      "A multi record batch appears together"
    );
    release();
    await batch;
    check(frame.revision === revision + 1, "A batch commits one Store revision");
    hold = false;
    await history.undo();
    check(
      first.getCachedRow(0).comment === "peer comment" && first.getCachedRow(1).comment === "",
      "One undo restores both edited records"
    );
    hold = true;
    const filter = { kind: "predicate", column: "label", operator: "eq", value: "urgent" };
    const filtered = await frame.createView(filter, JSON.stringify(filter), "id");
    await filtered.getRows(0, 1, signal);
    const moved = frame.applyEdit(first.getCachedRow(0), "label", "later");
    if (mode === "paged")
      check(
        filtered.getCachedRow(0).label === "later" && !filtered.disposed,
        "Paged filters keep the edited row while saving"
      );
    else {
      check(filtered.disposed, "Full sync updates affected queries immediately");
      const local = await frame.createView(filter, JSON.stringify(filter), "id");
      check(local.rowCount === 0, "Full sync filters query the optimistic SQLite value");
    }
    release();
    await moved;
    check(filtered.disposed && !first.disposed, "Acceptance invalidates only dependent queries");
    const replacement = await frame.createView(filter, JSON.stringify(filter), "id");
    check(replacement.rowCount === 0, "The accepted filter edit updates query membership");
    const sorted = await frame.createView(null, "null", "id", [{ column: "label", direction: "asc" }]);
    const unchanged = first.getCachedRow(0);
    const beforePeer = queryCalls;
    history.setConnected(false);
    const peer = { revision: await w.fixturePeer({ label: "first" }, "other598") };
    history.setConnected(true);
    for (let attempt = 0; attempt < 100 && frame.revision < peer.revision; attempt++)
      await new Promise((resolve) => setTimeout(resolve, 10));
    check(frame.revision === peer.revision, "The reader receives the peer revision");
    check(sorted.disposed && !first.disposed, "An offscreen sort edit invalidates only dependent queries");
    check(
      first.getCachedRow(0) === unchanged && queryCalls === beforePeer,
      "An unrelated loaded row keeps its object and does not reread"
    );
    if (mode === "paged")
      check(lastPayload.byte_count === 0, "Offscreen changes transfer summaries without row contents");
  } finally {
    history.commitRemoteOp = commit;
    history.getRemoteDataChanges = synchronize;
  }
}
