"""
Check database transactions, history, rollback and changes sent to browsers
"""

import decimal

import apsw
import pyarrow as pa
import pytest

from pret.dataframe_sqlite import INTERNAL_ROW_ID, SQLiteDataFrameProvider
from pret.store import StoreDatabase


def edit(provider, operation):
    """
    Save one DataFrame edit in its own Store transaction, as StoreDoc.dataframe_edit does
    """
    with provider.database.transaction(operation.get("request_id"), operation.get("origin")) as action:
        result = provider.apply_edit(operation)
        if result["status"] != "applied":
            raise ValueError("DataFrame edit conflicts with a newer action")
    return {**result, "revision": action["revision"]}


@pytest.fixture(params=[":memory:", "file"])
def database(request, tmp_path):
    """
    Run each database scenario in memory and in a temporary file
    """
    db = StoreDatabase(tmp_path / "store.sqlite" if request.param == "file" else ":memory:")
    yield db
    db.close()


def test_mixed_action_history_and_rollback(database):
    """
    Save ordinary values and two tables together, then check that a rejected edit changes none of them
    """
    with database.transaction(origin="initial"):
        database.ops = [{"op": "set", "path": ["data"], "value": {"state": {"flag": False}}}]
        docs = database.add_dataframe(pa.table({"label": ["old"]}), "docs")
        tags = database.add_dataframe(pa.table({"label": ["old"]}), "tags")
    with database.transaction("mixed", "michael"):
        database.ops = [{"op": "set", "path": ["data", "state", "flag"], "value": True}]
        for provider in (docs, tags):
            provider.apply_edit({"base_revision": 1, "row_identity": 1, "values": {"label": "new"}})
    assert database.revision == 2
    assert database.acknowledgment("mixed")["revision"] == 2
    assert database.acknowledgment("mixed")["row_identities"] == [[1], [1]]
    assert database.connection.execute("SELECT count(*) FROM _pret_acknowledgments WHERE revision=2").get == 1
    assert database.read_state() == ({"data": {"state": {"flag": True}}, "metadata": {}, "id": None}, 2)
    assert docs.materialize().to_pydict() == tags.materialize().to_pydict() == {"label": ["new"]}

    with pytest.raises(KeyError):
        with database.transaction("failed", "michael"):
            database.ops = [{"op": "set", "path": ["data", "state", "flag"], "value": False}]
            docs.apply_edit({"base_revision": 2, "row_identity": 1, "values": {"label": "bad"}})
            tags.apply_edit({"base_revision": 2, "row_identity": 99, "values": {"label": "bad"}})
    assert database.revision == 2
    assert database.acknowledgment("failed") is None
    assert docs.materialize().to_pydict() == {"label": ["new"]}
    assert database.read_state()[0]["data"]["state"]["flag"] is True


def test_selective_cells_same_value_peer_and_repeated_redo(database):
    """
    Repeated undo and redo must keep cells later edited by another user, even when their values match
    """
    provider = database.add_dataframe(pa.table({"label": ["old"], "score": [0.5]}), "docs")
    original = edit(
        provider,
        {
            "request_id": "michael",
            "origin": "michael",
            "base_revision": database.revision,
            "row_identity": 1,
            "values": {"label": "michael", "score": 0.8},
        },
    )
    assert database.acknowledgment("michael")["revision"] == original["revision"]
    edit(
        provider,
        {
            "origin": "dwight",
            "base_revision": database.revision,
            "row_identity": 1,
            "values": {"label": "michael"},
        },
    )
    action = original["revision"]
    for score in (0.5, 0.8, 0.5, 0.8):
        inverse = database.reverse(action, "michael")
        assert provider.materialize().to_pydict() == {"label": ["michael"], "score": [score]}
        action = inverse["revision"]
    undone = database.reverse(action, "michael")
    edit(
        provider,
        {
            "origin": "dwight",
            "base_revision": database.revision,
            "row_identity": 1,
            "values": {"score": 0.5},
        },
    )
    database.reverse(undone["revision"], "michael")
    assert provider.materialize().to_pydict() == {"label": ["michael"], "score": [0.5]}


def test_rows_and_invalid_batch(database):
    """
    Undo must keep inserted rows edited by another user, and invalid batches must change no rows
    """
    provider = database.add_dataframe(pa.table({"label": ["old"]}), "docs")
    insertion = edit(
        provider,
        {
            "origin": "michael",
            "base_revision": database.revision,
            "kind": "insert",
            "values": {"label": "new"},
        },
    )
    edit(
        provider,
        {
            "origin": "dwight",
            "base_revision": database.revision,
            "row_identity": insertion["row_identity"],
            "values": {"label": "new"},
        },
    )
    database.reverse(insertion["revision"], "michael")
    assert provider.describe()["row_count"] == 2
    deletion = edit(
        provider,
        {
            "origin": "michael",
            "base_revision": database.revision,
            "kind": "delete",
            "row_identity": 1,
        },
    )
    restore = database.reverse(deletion["revision"], "michael")
    assert provider.describe()["row_count"] == 2
    database.reverse(restore["revision"], "michael")
    inserted = edit(
        provider,
        {
            "base_revision": database.revision,
            "kind": "insert",
            "values": {"label": "fresh"},
        },
    )
    assert inserted["row_identity"] == 3
    before = provider.materialize()
    revision = database.revision
    with pytest.raises(KeyError):
        edit(
            provider,
            {
                "base_revision": revision,
                "kind": "batch",
                "edits": [
                    {"row_identity": 2, "values": {"label": "changed"}},
                    {"row_identity": 999, "values": {"label": "missing"}},
                ],
            },
        )
    assert database.revision == revision and provider.materialize().equals(before)
    with pytest.raises(RuntimeError, match="nested"):
        with database.transaction():
            try:
                provider.apply_edit({"base_revision": revision, "row_identity": 999, "values": {"label": "bad"}})
            except KeyError:
                pass
    assert database.revision == revision
    empty = database.add_dataframe(pa.table({"value": [1, 2]}).select([]), "empty")
    inserted = edit(empty, {"base_revision": database.revision, "kind": "insert", "values": {}})
    assert empty.describe()["row_count"] == 3
    database.reverse(inserted["revision"], "python")
    assert empty.describe()["row_count"] == 2


def test_full_queries_locations_distinct_and_relations(database):
    """
    Filters, sorting and selection lookup must include rows outside the first page
    """
    provider = database.add_dataframe(
        pa.table({"key": range(1000), "score": [idx % 13 for idx in range(1000)]}), "docs"
    )
    parent = database.add_dataframe(pa.table({"key": [901, 950, 999], "selected": [True, False, True]}), "parent")
    options = {
        "filter": {"kind": "predicate", "column": "key", "operator": "gte", "value": 900},
        "sort": [{"column": "score", "direction": "desc"}],
        "columns": ["key"],
        "offset": 7,
        "limit": 5,
        "locate": {"column": "key", "values": [901, 999, 10]},
    }
    result = provider.query(options)
    expected = sorted(range(900, 1000), key=lambda idx: (-(idx % 13), idx))
    assert result["total_count"] == 100
    assert result["table"].column("key").to_pylist() == expected[7:12]
    assert result["table"].column_names == [INTERNAL_ROW_ID, "key"]
    assert result["positions"] == [expected.index(901), expected.index(999), None]
    assert provider.query({"distinct": "score"})["total_count"] == 13
    relation = {
        "kind": "relation",
        "column": "key",
        "resource_id": parent.resource_id,
        "parent_column": "key",
        "filter": {"kind": "predicate", "column": "selected", "operator": "eq", "value": True},
    }
    assert provider.query({"filter": relation})["table"].column("key").to_pylist() == [901, 999]
    assert provider.query({"identities": []})["total_count"] == 0
    with pytest.raises(ValueError):
        provider.query({"sort": [{"column": "missing", "direction": "asc"}]})


def test_exact_arrow_types_and_decimal_order(database):
    """
    Saving and querying must preserve large numbers and timestamp precision
    """
    table = pa.table(
        {
            "small_decimal": pa.array(
                [decimal.Decimal(value) for value in ["-10.00", "2.50", "10.00"]], pa.decimal128(38, 2)
            ),
            "large_decimal": pa.array(
                [
                    decimal.Decimal(value)
                    for value in [
                        "-" + "9" * 70 + ".123456",
                        "0.000001",
                        "9" * 70 + ".123456",
                    ]
                ],
                pa.decimal256(76, 6),
            ),
            "uint": pa.array([0, 2**63, 2**64 - 1], pa.uint64()),
            "signed": pa.array([-(2**63), 2**53 + 1, 2**63 - 1], pa.int64()),
            "time": pa.array([1, 2, 3], pa.time64("ns")),
            "timestamp": pa.array([1789381234567890123 + idx for idx in range(3)], pa.timestamp("ns", "UTC")),
            "nested": pa.array(
                [{"bytes": [b"\x00\xff", None], "number": 2**63 + 1}, None, {"bytes": [], "number": 0}],
                pa.struct([("bytes", pa.list_(pa.binary())), ("number", pa.uint64())]),
            ),
        }
    )
    provider = database.add_dataframe(table, "typed")
    assert provider.materialize().equals(table)
    for column in ("small_decimal", "large_decimal", "uint", "signed", "time", "timestamp"):
        assert (
            provider.query({"sort": [{"column": column, "direction": "asc"}]})["table"]
            .column(column)
            .equals(table[column])
        )
    result = provider.query(
        {"filter": {"kind": "predicate", "column": "small_decimal", "operator": "gt", "value": "2.50"}}
    )
    assert result["table"].column("small_decimal").to_pylist() == [decimal.Decimal("10.00")]
    edit(
        provider, {"base_revision": database.revision, "row_identity": 1, "values": {"time": 7, "uint": str(2**64 - 1)}}
    )
    assert provider.materialize().column("time").cast(pa.int64()).to_pylist()[0] == 7


def test_live_store_rollback_preserves_root_and_nested_reference():
    """
    After SQLite rejects an edit, existing Python references see the restored values and nothing is saved
    """
    from pret import DataFrame, create_store
    from pret.store import transact

    store = create_store({"a": {"b": 1}, "rows": DataFrame([{"id": 1, "x": 2}])})
    nested = store["a"]
    database = store.doc.database
    try:
        with pytest.raises(pa.ArrowInvalid):
            with transact(store):
                nested["b"] = 8
                store["rows"].update(identity={"id": 1}, values={"x": "wrong"})
        assert nested["b"] == store["a"]["b"] == 1
        assert store["rows"].materialize().to_pydict() == {"id": [1], "x": [2]}
        assert database.revision == 1
        assert database.read_state()[0]["data"]["a"]["b"] == 1
        nested["b"] = 9
        assert store["a"]["b"] == 9
    finally:
        database.close()


def test_failed_rollback_blocks_further_store_edits(monkeypatch):
    """
    Keep editing disabled if the values of a failed commit cannot be restored, a rejected commit alone does not
    """
    from pret import create_store, store_ops

    store = create_store({"a": {"b": 1}})
    database = store.doc.database
    failing = []
    original = store_ops.apply

    def apply(*args, **kwargs):
        if failing:
            raise RuntimeError("broken")
        return original(*args, **kwargs)

    monkeypatch.setattr(store_ops, "apply", apply)
    try:
        database.connection.set_commit_hook(lambda: True)
        with pytest.raises(apsw.ConstraintError):
            store["a"]["b"] = 8
        assert store["a"]["b"] == 1 and store.doc.error is None
        database.connection.set_commit_hook(lambda: failing.append(True) or True)
        with pytest.raises(RuntimeError, match="broken"):
            store["a"]["b"] = 8
        assert store.doc.error is not None
        database.connection.set_commit_hook(None)
        with pytest.raises(RuntimeError, match="could not be restored"):
            store["a"]["b"] = 9
    finally:
        database.connection.set_commit_hook(None)
        database.close()


def test_streamed_import_and_nan_rejection(database):
    """
    Import Arrow batches without collecting them all and reject NaN rather than converting it to null
    """
    schema = pa.schema([("value", pa.int64())])
    batches = (pa.record_batch([pa.array(range(start, start + 2049))], schema=schema) for start in (0, 2049))
    reader = pa.RecordBatchReader.from_batches(schema, batches)
    provider = database.add_dataframe(reader, "stream")
    assert provider.query({"offset": 4096, "limit": 2})["table"]["value"].to_pylist() == [4096, 4097]
    before = database.revision
    with pytest.raises(ValueError, match="NaN"):
        database.add_dataframe(pa.table({"value": [float("nan")]}), "nan")
    assert database.revision == before
    with pytest.raises(KeyError):
        SQLiteDataFrameProvider(database, "nan")
    database.connection.set_commit_hook(lambda: True)
    try:
        with pytest.raises(apsw.ConstraintError):
            with database.transaction():
                database.ops = [{"op": "set", "path": ["data"], "value": 1}]
        assert database.revision == before
    finally:
        database.connection.set_commit_hook(None)


def test_nested_temporal_units_and_time_filters(database):
    """
    Saving and filtering nested timestamps must keep nanoseconds that Python datetime cannot represent
    """
    schema = pa.schema([("nested", pa.list_(pa.struct([("time", pa.time64("ns"))])))])
    table = pa.Table.from_pylist([{"nested": [{"time": 1}, {"time": 2}]}], schema=schema)
    provider = database.add_dataframe(table, "nested_time")
    assert provider.materialize().equals(table)
    edit(provider, {"base_revision": database.revision, "row_identity": 1, "values": {"nested": [{"time": 7}]}})
    expected = pa.Table.from_pylist([{"nested": [{"time": 7}]}], schema=schema)
    assert provider.materialize().equals(expected)
    times = database.add_dataframe(pa.table({"time": pa.array([1, 2, 3], pa.time64("ns"))}), "times")
    result = times.query({"filter": {"kind": "predicate", "column": "time", "operator": "gt", "value": 1}})
    assert result["table"]["time"].cast(pa.int64()).to_pylist() == [2, 3]


@pytest.mark.parametrize(
    "arrow_type",
    [
        pa.dictionary(pa.int8(), pa.string()),
        pa.month_day_nano_interval(),
        pa.list_(pa.dictionary(pa.int8(), pa.string())),
    ],
)
def test_unsupported_arrow_schema_rejected_before_import(database, arrow_type):
    """
    Reject unsupported column types even in an empty table
    """
    before = database.revision
    with pytest.raises(TypeError, match="does not support Arrow type"):
        database.add_dataframe(pa.table({"value": pa.array([], type=arrow_type)}), "unsupported")
    assert database.revision == before
    assert database.connection.execute("SELECT count(*) FROM _pret_dataframes").get == 0


def test_failed_dataframe_assignment_drops_uncommitted_browser_resources():
    """
    After an edit is rejected, keep the old values and do not send its new DataFrames to browsers
    """
    from pret import DataFrame, create_store
    from pret.store import transact

    store = create_store({"rows": DataFrame({"value": [1]}), "label": "original"}, sync=True)
    original = store["rows"]
    replacement = DataFrame({"value": [2]})
    try:
        with pytest.raises(ValueError, match="invalid later edit"):
            with transact(store):
                store["rows"] = replacement
                store["label"] = "changed"
                raise ValueError("invalid later edit")
        assert store["label"] == "original"
        assert store["rows"].resource_id == original.resource_id
        assert store["rows"].materialize().to_pydict() == {"value": [1]}
        assert replacement.resource_id not in store.doc.__reduce__()[1][2]
        assert replacement.resource_id not in store.doc._dataframe_registry
        store["rows"] = replacement
        assert store["rows"].materialize().to_pydict() == {"value": [2]}
    finally:
        store.doc.database.close()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nested_nonfinite_float_rejected(database, value):
    """
    Reject nested floating point values that browser JSON cannot preserve
    """
    with pytest.raises(ValueError, match="NaN|nonfinite"):
        database.add_dataframe(pa.table({"nested": [[value]]}), "nonfinite")


def test_dataframe_aliases_share_one_materialization_and_history(tmp_path):
    """
    Two references to the same DataFrame in a Store must share one table and its edits
    """
    from pret import DataFrame, create_store
    from pret.store import transact

    source = DataFrame({"id": [1], "value": [0]})
    path = tmp_path / "aliases.sqlite"
    store = create_store({"first": source, "second": source, "label": "old"}, sync=path)
    database = store.doc.database
    with transact(store):
        store["third"] = store["first"]
        store["first"].update(identity={"id": 1}, values={"value": 1})
        store["second"].update(identity={"id": 1}, values={"value": 2})
        store["label"] = "new"
    copied = create_store({"copy": store["first"]})
    try:
        assert source.store_doc is None
        assert database.connection.execute("SELECT count(*) FROM _pret_dataframes").get == 1
        assert store["first"] is store["second"] is store["third"]
        assert store["third"].materialize().to_pydict()["value"] == [2]
        changeset = database.connection.execute(
            "SELECT changeset FROM _pret_changes WHERE revision=?", (store.revision,)
        ).get
        assert len(list(apsw.Changeset.iter(changeset))) == 1
        copied["copy"].update(identity={"id": 1}, values={"value": 3})
        assert store["first"].store_doc is store.doc
        assert copied["copy"].store_doc is copied.doc
        assert store["first"].materialize().to_pydict()["value"] == [2]
    finally:
        copied.doc.database.close()
        database.close()


def test_complete_copy_transport(tmp_path):
    """
    Copy one committed Store revision then transfer typed edits and newly added tables
    """
    from pret import DataFrame, create_store, load_store, save_store
    from pret.store import transact

    with pytest.raises(ValueError, match="positive dataframe_max_bytes"):
        create_store({}, sync=True, dataframe_sync="full")
    source = create_store(
        {
            "reviewed": False,
            "rows": DataFrame(
                pa.table(
                    {
                        "id": [1, 2],
                        "integer": pa.array([9007199254740993, 2], type=pa.int64()),
                        "instant": pa.array([1789381234567890123, None], type=pa.timestamp("ns")),
                        "amount": pa.array([decimal.Decimal("1.23"), None], type=pa.decimal128(38, 2)),
                        "nested": pa.array([[{"text": "old"}], []]),
                        "binary": [b"\x00\xff", None],
                    }
                )
            ),
        },
        sync=True,
        dataframe_sync="full",
        dataframe_max_bytes=100000,
    )
    doc = source.doc
    try:
        snapshot = doc.browser_data()
        assert snapshot.data["revision"] == source.revision
        assert len(snapshot.data["tables"]) == 1
        with pytest.raises(ValueError, match="download exceeds"):
            doc.database.browser_data(None, 1)
        with transact(source):
            source["reviewed"] = True
            source["rows"].update(
                identity={"id": 1},
                values={
                    "integer": 9007199254740995,
                    "instant": 1789381234567890125,
                    "amount": "9.87",
                    "nested": [{"text": "new"}],
                    "binary": b"\x01\xfe",
                },
            )
        changes = doc.browser_data(snapshot.data["revision"])
        assert changes.data["tables"] == []
        (commit,) = changes.data["commits"]
        (change,) = commit["changes"]
        table = pa.ipc.open_stream(changes.buffers[0]).read_all()
        assert change["kind"] == "update"
        assert table["integer"][0].as_py() == 9007199254740995
        assert table["instant"].cast(pa.int64())[0].as_py() == 1789381234567890125
        assert table["amount"][0].as_py() == decimal.Decimal("9.87")
        assert table["nested"][0].as_py() == [{"text": "new"}]
        assert table["binary"][0].as_py() == b"\x01\xfe"
        assert doc.browser_data(commit["revision"]).data["commits"] == []
        source["new"] = DataFrame([{"label": "initial"}])
        source["new"].update(identity={"label": "initial"}, values={"label": "latest"})
        added = doc.browser_data(commit["revision"])
        assert len(added.data["tables"]) == 1
        assert pa.ipc.open_stream(added.buffers[0]).read_all()["label"].to_pylist() == ["latest"]
        assert all(not item["changes"] for item in added.data["commits"])
        path = tmp_path / "full.sqlite"
        saved = save_store(source, path)
        loaded = load_store(path, dataframe_sync="full", dataframe_max_bytes=100000)
        assert loaded.doc.dataframe_sync == "full"
        assert loaded.doc.browser_data().data["revision"] == loaded.revision
        saved.doc.database.close()
        loaded.doc.database.close()
    finally:
        doc.database.close()


def test_complete_copy_during_peer_edit(tmp_path, monkeypatch):
    """
    Keep copied rows and ordinary values at one revision when another connection edits during Arrow encoding
    """
    import pret.store as store_module
    from pret import DataFrame, create_store, load_store
    from pret.store import transact

    writer = create_store({"value": 1, "rows": DataFrame([{"id": 1, "value": 1}])}, sync=tmp_path / "copy.sqlite")
    reader = load_store(tmp_path / "copy.sqlite")
    # WAL allows the test writer to commit before the reader finishes copying
    for store in (writer, reader):
        store.doc.database.connection.execute("PRAGMA journal_mode=WAL")
    serialize, wrote = store_module.serialize_arrow_as_bytes, False

    def encode(table):
        """
        Save a mixed peer action between copying table rows and reading ordinary values
        """
        nonlocal wrote
        if not wrote:
            wrote = True
            with transact(writer):
                writer["value"] = 2
                writer["rows"].update(identity={"id": 1}, values={"value": 2})
        return serialize(table)

    monkeypatch.setattr(store_module, "serialize_arrow_as_bytes", encode)
    try:
        snapshot = reader.doc.browser_data()
        assert wrote
        assert snapshot.data["state"]["data"]["value"] == 1
        assert pa.ipc.open_stream(snapshot.buffers[0]).read_all()["value"].to_pylist() == [1]
        changes = reader.doc.browser_data(snapshot.data["revision"])
        assert len(changes.data["commits"]) == 1
        assert changes.data["commits"][0]["ops"] == [{"op": "set", "path": ["data", "value"], "value": 2}]
        assert pa.ipc.open_stream(changes.buffers[0]).read_all()["value"].to_pylist() == [2]
    finally:
        for store in (reader, writer):
            store.doc.database.close()


def test_subscribed_row_changes(database):
    """
    Transfer loaded row values while reporting offscreen changes needed to maintain queries
    """
    with database.transaction("initial", "michael"):
        provider = database.add_dataframe(pa.table({"label": ["later", "later"], "rank": [1, 2]}), "todos")
    revision = database.revision
    initial = database.browser_data(None, None, {"todos": [1]})
    assert initial.data["tables"] == []
    assert initial.data["byte_count"] == 0
    with database.transaction("edit", "michael"):
        provider.apply_edit({"base_revision": revision, "row_identity": 1, "values": {"label": "urgent"}})
        provider.apply_edit({"base_revision": revision, "row_identity": 2, "values": {"rank": 0}})
    response = database.browser_data(revision, None, {"todos": [1]})
    (commit,) = response.data["commits"]
    assert commit["summaries"] == [{"resource_id": "todos", "kind": "update", "columns": ["label", "rank"], "count": 2}]
    (change,) = commit["changes"]
    rows = pa.ipc.open_stream(response.buffers[0]).read_all().to_pylist()
    assert rows == [{INTERNAL_ROW_ID: 1, "label": "urgent"}]
    assert change["resource_id"] == "todos"
    unseen = database.browser_data(revision, None, {})
    assert unseen.data["commits"][0]["summaries"] == commit["summaries"]
    assert unseen.data["commits"][0]["changes"] == []
    assert unseen.data["byte_count"] == 0
