import * as arrow from "apache-arrow";
import { createBrowserDatabase } from "../../client/dataframeSqlite";
import { decodeDataFrameRows } from "../../client/dataframeValueConversions";
import { arrowValue, parseCellValue } from "../../client/dataframeValueConversions";
import { runStoreSessionTests } from "./store.browser";

// Check local queries and pending edits with real SQLite WASM, using Python Arrow bytes to catch type conversion errors
export async function runDataFrameTests(typedBytes: number[]) {
  function check(value, name) {
    if (!value) throw new Error(name);
  }
  const table = new arrow.Table({
    id: arrow.vectorFromArray(
      Array.from({ length: 1000 }, (_, idx) => idx + 1),
      new arrow.Int32()
    ),
    label: arrow.vectorFromArray(
      Array.from({ length: 1000 }, (_, idx) => (idx % 2 ? "odd" : "even")),
      new arrow.Utf8()
    ),
    score: arrow.vectorFromArray(
      Array.from({ length: 1000 }, (_, idx) => idx % 13),
      new arrow.Float64()
    ),
  });
  const resources = [
    { resource_id: "docs", bytes: arrow.tableToIPC(table) },
    { resource_id: "typed", bytes: new Uint8Array(typedBytes) },
  ];
  const db = await createBrowserDatabase(resources);
  // Browser only Stores keep their edits, connected full copies roll them back once Python replies
  function edit(resource, operation) {
    db.begin();
    const result = db.applyEdit(resource, operation, false);
    db.release();
    return result;
  }
  const row = () => decodeDataFrameRows(db.query("docs", { identities: [1] }).bytes)[0];
  try {
    const filter = { kind: "predicate", column: "id", operator: "gt", value: 900 };
    const queried = db.query("docs", { filter, sort: [{ column: "score", direction: "desc" }], offset: 7, limit: 7 });
    const expected = Array.from({ length: 100 }, (_, idx) => ({ id: idx + 901, score: (idx + 900) % 13 }))
      .sort((a, b) => b.score - a.score || a.id - b.id)
      .slice(7, 14)
      .map((value) => value.id);
    check(
      JSON.stringify(decodeDataFrameRows(queried.bytes).map((value) => value.id)) === JSON.stringify(expected),
      "filters and stable paging query all rows"
    );
    check(queried.total_count === 100 && queried.row_count === 7, "filtered count is independent of page size");
    const distinct = db.query("docs", { distinct: "label", limit: 1, offset: 1 });
    check(
      distinct.total_count === 2 && decodeDataFrameRows(distinct.bytes)[0].label === "odd",
      "distinct uses full data with paging"
    );
    const located = db.query("docs", {
      filter,
      sort: [{ column: "id", direction: "desc" }],
      locate: { column: "id", values: [1000, 901, 1] },
    });
    check(
      JSON.stringify(located.positions) === "[0,99,null]",
      "locate returns positions in the filtered sorted result"
    );
    const relation = db.query("docs", {
      filter: { kind: "relation", resource_id: "docs", column: "id", parent_column: "id", filter },
      limit: 2,
    });
    check(
      relation.total_count === 100 && decodeDataFrameRows(relation.bytes)[0].id === 901,
      "relation filtering stays in the Store database"
    );

    edit("docs", { kind: "update", row_identity: 1, values: { label: "michael", score: 0.8 } });
    check(row().label === "michael" && row().score === 0.8, "released edits are kept");
    db.begin();
    db.applyEdit("docs", { kind: "update", row_identity: 1, values: { score: 99 } }, true);
    db.applyEdit("typed", { kind: "update", row_identity: 1, values: { integer: "9007199254740999" } }, true);
    const pending = db.applyEdit("docs", { kind: "insert", values: { id: 1001, label: "new", score: 1 } }, true);
    check(row().score === 99 && pending.row_identity < 0, "pending edits are visible and inserts get temporary IDs");
    let invalid = false;
    try {
      db.applyEdit("docs", { kind: "batch", edits: [
        { kind: "update", row_identity: 2, values: { score: 9 } },
        { kind: "update", row_identity: 1, values: { missing: 4 } },
      ] }, true);
    } catch {
      invalid = true;
    }
    check(
      invalid && decodeDataFrameRows(db.query("docs", { identities: [2] }).bytes)[0].score === 1,
      "an invalid batch leaves none of its edits"
    );
    db.rollback();
    check(
      row().score === 0.8 && db.query("docs", { identities: [pending.row_identity] }).row_count === 0,
      "rollback discards pending edits across resources"
    );
    const removed = edit("docs", { kind: "delete", row_identity: 1000 });
    check(
      removed.row_identity === 1000 && db.query("docs", { identities: [1000] }).row_count === 0,
      "deletion removes the requested identity"
    );

    const typed = arrow.tableFromIPC(db.query("typed", { limit: 10 }).bytes);
    check(arrowValue(typed.getChild("integer"), 0) === BigInt("9007199254740993"), "signed int64 is exact");
    check(arrowValue(typed.getChild("unsigned"), 0) === BigInt("18446744073709551615"), "unsigned int64 is exact");
    check(arrowValue(typed.getChild("instant"), 0) === BigInt("1789381234567890123"), "nanosecond timestamp is exact");
    check(
      arrowValue(typed.getChild("amount"), 0) === BigInt("99999999999999999999999999999999999999"),
      "decimal128 coefficient is exact"
    );
    const nested = arrowValue(typed.getChild("nested"), 0);
    check(
      nested[0].instant === BigInt("1789381234567890123") && nested[0].binary[1] === 255 && nested[0].missing === null,
      "nested temporal binary and null values round trip"
    );
    check(
      db.query("typed", { filter: { kind: "predicate", column: "amount", operator: "gt", value: "0.00" } })
        .total_count === 1,
      "decimal filters use exact numeric order"
    );
    check(
      decodeDataFrameRows(db.query("typed", { sort: [{ column: "unsigned", direction: "asc" }] }).bytes)[0].unsigned ===
        1,
      "unsigned integers sort numerically"
    );
    edit("typed", {
      kind: "update",
      row_identity: 1,
      values: {
        integer: "9223372036854775807",
        unsigned: "18446744073709551614",
        instant: "2026-09-14T09:00:00.123456789Z",
        amount: "1.23",
      },
    });
    const after = arrow.tableFromIPC(db.query("typed", { identities: [1] }).bytes);
    check(
      arrowValue(after.getChild("integer"), 0) === BigInt("9223372036854775807"),
      "typed edits preserve signed limits"
    );
    check(
      String(arrowValue(after.getChild("instant"), 0)).endsWith("123456789"),
      "timestamp string edits retain nanoseconds"
    );
    const display = decodeDataFrameRows(db.query("typed", { identities: [1] }).bytes)[0];
    check(
      display.amount === "1.23" &&
        display.instant === "2026-09-14T09:00:00.123456789Z" &&
        Array.isArray(display.nested) &&
        typeof display.nested[0].instant === "string" &&
        display.nested[0].binary === "AP+A" &&
        display.nested[0].missing === null,
      "display codec gives widgets scalars as text and nested values as arrays and objects of them"
    );
    for (const value of [NaN, Infinity, -Infinity]) {
      const field = new arrow.Field("value", new arrow.Float64(), true);
      const nestedType = new arrow.List(new arrow.Field("item", new arrow.Struct([field]), true));
      const nonfinite = new arrow.Table({ nested: arrow.vectorFromArray([[{ value }]], nestedType) });
      let rejected = false;
      try {
        const imported = await createBrowserDatabase([
          { resource_id: "nonfinite", bytes: arrow.tableToIPC(nonfinite) },
        ]);
        imported.close();
      } catch (error) {
        rejected = error.message.includes("Nested nonfinite values");
      }
      check(rejected, `nested ${String(value)} import rejects JSON null conversion`);
    }
    for (const [type, value] of [
      [new arrow.TimestampNanosecond(), Number.MAX_SAFE_INTEGER + 1],
      [new arrow.DateDay(), "2147483648"],
      [new arrow.TimeSecond(), "-2147483649"],
      [new arrow.TimestampNanosecond(), "9223372036854775808"],
      [new arrow.TimestampNanosecond(), "9999-01-01T00:00:00Z"],
    ] as const) {
      let rejected = false;
      try {
        parseCellValue(value, new arrow.Field("temporal", type, true));
      } catch (error) {
        rejected = error.message.includes("integer");
      }
      check(rejected, `${String(type)} rejects lossy temporal count ${String(value)}`);
    }
    await runStoreSessionTests();
  } finally {
    db.close();
  }
}
