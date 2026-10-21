"""
Check source queries, filter validation and exact values exchanged with the browser
"""

import asyncio
import gc

import ibis
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest

from pret import DataFrame, DataFrameProvider, apply_ibis_filter, create_store, save_store
from pret.dataframe import IbisDataFrameProvider
from pret.manager import get_manager

TABLE = ibis.table(
    {
        "list_id": "int64",
        "active": "boolean",
        "name": "string",
        "day": "date",
        "payload": "array<string>",
    },
    name="lists",
)


def test_python_crud_and_queries_use_public_column_identity(tmp_path):
    path = tmp_path / "review.pret"
    dataframe = save_store(
        create_store(
            {
                "annotations": DataFrame(
                    [
                        {"todo_id": "t1", "label": "A", "reviewed": False},
                        {"todo_id": "t2", "label": "B", "reviewed": False},
                    ]
                )
            },
            sync=True,
        ),
        path,
    )["annotations"]

    dataframe.update(identity={"todo_id": "t1"}, values={"reviewed": True})
    dataframe.insert({"todo_id": "t3", "label": "A", "reviewed": False})
    dataframe.delete(identity={"todo_id": "t2"})
    filtered = dataframe.provider.query(
        {"filter": {"kind": "predicate", "column": "label", "operator": "eq", "value": "A"}}
    )["table"]

    assert filtered.select(["todo_id", "reviewed"]).to_pylist() == [
        {"todo_id": "t1", "reviewed": True},
        {"todo_id": "t3", "reviewed": False},
    ]


def test_typed_edits_validate_whole_action_and_nested_nullability(tmp_path):
    schema = pa.schema(
        [
            pa.field("number", pa.int64(), nullable=False),
            pa.field(
                "nested",
                pa.list_(
                    pa.field(
                        "element",
                        pa.struct(
                            [
                                pa.field("label", pa.string(), nullable=False),
                            ]
                        ),
                        nullable=False,
                    )
                ),
            ),
        ]
    )
    saved = save_store(
        create_store(
            {
                "rows": DataFrame(
                    pa.Table.from_pylist(
                        [
                            {"number": 1, "nested": [{"label": "a"}]},
                            {"number": 2, "nested": [{"label": "b"}]},
                        ],
                        schema=schema,
                    )
                )
            },
            sync=True,
        ),
        tmp_path / "typed.pret",
    )
    provider = saved["rows"].provider
    initial_revision = provider.database.revision
    for column, value in [("number", "invalid"), ("number", None), ("nested", [{"label": None}])]:
        with pytest.raises((ValueError, pa.ArrowException)):
            saved.doc.dataframe_edit(
                saved["rows"].resource_id,
                {
                    "kind": "batch",
                    "base_revision": 0,
                    "edits": [
                        {"row_identity": 1, "column": "number", "value": "10"},
                        {"row_identity": 2, "column": column, "value": value},
                    ],
                },
            )
        assert provider.database.revision == initial_revision
        assert provider.query()["table"]["number"].to_pylist() == [1, 2]
    with pytest.raises(ValueError, match="null"):
        saved.doc.dataframe_edit(saved["rows"].resource_id, {"kind": "insert", "base_revision": 0, "values": {}})
    edit = {
        "request_id": "typed-number",
        "base_revision": 0,
        "row_identity": 1,
        "column": "number",
        "value": "42",
    }
    store_revision = provider.database.revision
    applied = saved.doc.dataframe_edit(saved["rows"].resource_id, edit)
    assert saved.doc.dataframe_edit(saved["rows"].resource_id, edit) == applied
    assert provider.database.revision == store_revision + 1
    assert provider.query()["table"]["number"].to_pylist() == [42, 2]


def test_nested_values_roundtrip_and_accept_widget_json(tmp_path):
    import datetime
    import decimal

    schema = pa.schema(
        [
            pa.field(
                "nested",
                pa.list_(
                    pa.struct(
                        [
                            pa.field("when", pa.timestamp("us")),
                            pa.field("amount", pa.decimal128(10, 2)),
                            pa.field("data", pa.binary()),
                        ]
                    )
                ),
            )
        ]
    )
    values = [
        {
            "when": datetime.datetime(2026, 9, 7, 12),
            "amount": decimal.Decimal("1.25"),
            "data": b"abc",
        }
    ]
    table = pa.Table.from_pylist([{"nested": values}], schema=schema)
    saved = save_store(create_store({"rows": DataFrame(table)}, sync=True), tmp_path / "nested.pret")
    provider = saved["rows"].provider
    assert provider.materialize().equals(table)
    saved.doc.dataframe_edit(
        saved["rows"].resource_id,
        {
            "base_revision": 0,
            "row_identity": 1,
            "column": "nested",
            "value": '[{"when":"2026-09-08T12:00:00","amount":"2.50","data":"ZGVm"}]',
        },
    )
    assert provider.materialize().to_pylist() == [
        {
            "nested": [
                {
                    "when": datetime.datetime(2026, 9, 8, 12),
                    "amount": decimal.Decimal("2.50"),
                    "data": b"def",
                }
            ]
        }
    ]


def test_normalized_nested_and_fixed_binary_edit_roundtrip(tmp_path):
    table = pa.table(
        {
            "nested": pa.array([[{"data": b"abc"}]], type=pa.list_(pa.struct([("data", pa.binary())]))),
            "fixed": pa.array([b"xyz"], type=pa.binary(3)),
        }
    )
    saved = save_store(create_store({"rows": DataFrame(table)}, sync=True), tmp_path / "binary.pret")
    dataframe = saved["rows"]
    payload = asyncio.run(dataframe.query({"limit": 1}))
    raw = ipc.open_stream(pa.py_buffer(payload.buffers[0])).read_all().to_pylist()[0]
    assert raw["nested"] == [{"data": b"abc"}]
    assert raw["fixed"] == b"xyz"
    asyncio.run(
        dataframe.apply_edit(
            {
                "base_revision": payload.data["revision"],
                "row_identity": raw["__pret_row_id"],
                "values": {"nested": '[{"data":"YWJj"}]', "fixed": "eHl6"},
            }
        )
    )
    assert dataframe.provider.materialize().equals(table)


def test_custom_provider_requires_reads_and_defaults_to_read_only():
    with pytest.raises(TypeError, match="abstract"):
        DataFrameProvider()

    class ReadOnlyRows(DataFrameProvider):
        def describe(self):
            return {
                "columns": [{"name": "id", "type": "int64", "nullable": False}],
                "row_count": 1,
                "capabilities": {"edit": False},
            }

        async def query(self, options=None):
            return {"table": pa.table({"id": [42]}), "total_count": 1}

    provider = ReadOnlyRows()
    frame = DataFrame(provider=provider)
    assert asyncio.run(frame.describe())["row_count"] == 1
    payload = asyncio.run(frame.query())
    assert ipc.open_stream(payload.buffers[0]).read_all().to_pylist() == [{"id": 42}]
    with pytest.raises(NotImplementedError, match="read only"):
        provider.apply_edit({"kind": "delete", "row_identity": 42})
    with pytest.raises(NotImplementedError, match="materialize"):
        frame.materialize()


def test_changed_revision_invalidates_pages_without_sending_full_rows(tmp_path):
    frame = save_store(
        create_store({"rows": DataFrame([{"nested": [{"value": "one"}], "binary": b"abc"}])}, sync=True),
        tmp_path / "rows.pret",
    )["rows"]
    frame.update(identity={"binary": b"abc"}, values={"nested": [{"value": "two"}]})

    snapshot = asyncio.run(frame.query({"limit": 10}))
    changes = asyncio.run(frame.get_changes_after_revision(0))
    snapshot_table = ipc.open_stream(snapshot.buffers[0]).read_all()
    assert changes == {"revision": snapshot.data["revision"], "refresh": True}
    assert snapshot_table["nested"].to_pylist() == [[{"value": "two"}]]
    assert snapshot_table["binary"].to_pylist() == [b"abc"]


def test_kernel_bindings_keep_provider_methods_separate():
    todos = DataFrame([{"todo_id": 1, "text": "Buy milk"}])
    tags = DataFrame([{"todo_id": 1, "start": 0, "end": 4}])

    store = create_store({"todos": todos, "tags": tags}, sync=True)
    todos, tags = store["todos"], store["tags"]
    todos.__reduce__()
    tags.__reduce__()
    manager = get_manager()

    todos_description = asyncio.run(manager.functions[f"dataframe:{todos.resource_id}:{todos.binding_id}:describe"]())
    tags_description = asyncio.run(manager.functions[f"dataframe:{tags.resource_id}:{tags.binding_id}:describe"]())

    assert [column["name"] for column in todos_description["columns"]] == [
        "todo_id",
        "text",
    ]
    assert [column["name"] for column in tags_description["columns"]] == [
        "todo_id",
        "start",
        "end",
    ]


def test_query_filters_and_sorts_before_paging_and_preserves_exact_types():
    frame = DataFrame(pa.table({"id": range(1000), "value": [2**60 + index for index in range(1000)]}))
    payload = asyncio.run(
        frame.query(
            {
                "filter": {"kind": "predicate", "column": "id", "operator": "gte", "value": 990},
                "sort": [{"column": "id", "direction": "desc"}],
                "columns": ["value"],
                "offset": 1,
                "limit": 2,
                "locate": {"column": "id", "values": [995, 123]},
            }
        )
    )
    rows = ipc.open_stream(payload.buffers[0]).read_all()
    assert payload.data["total_count"] == 10
    assert payload.data["positions"] == [4, None]
    assert rows["value"].to_pylist() == [2**60 + 998, 2**60 + 997]


@pytest.mark.parametrize("engine", ["sqlite", "pyspark"])
def test_ibis_source_previews_and_explicit_paging(engine, request):
    """
    Share source paging checks across SQLite and raw Spark inputs and preserve exact Arrow values
    """
    ibis = pytest.importorskip("ibis")
    if engine == "pyspark":
        pytest.importorskip("ibis.backends.pyspark")
        from pyspark.sql import SparkSession

        backend = (
            SparkSession.builder.master("local[1]")
            .appName("pret test")
            .config("spark.sql.session.timeZone", "Europe/Paris")
            .getOrCreate()
        )
        request.addfinalizer(backend.stop)
        backend.sparkContext.setLogLevel("ERROR")
        views = backend.catalog.listTables()
        source = backend.sql("SELECT * FROM VALUES (1,'one'),(2,'two'),(3,'three') AS data(id,label)")
    else:
        backend = ibis.sqlite.connect(":memory:")
        request.addfinalizer(backend.disconnect)
        backend.raw_sql("CREATE TABLE data(id INTEGER, label TEXT)")
        backend.raw_sql("INSERT INTO data VALUES (1,'one'),(2,'two'),(3,'three')")
        source = backend.table("data")
    frame = DataFrame(source)
    preview = frame.provider
    assert isinstance(preview, IbisDataFrameProvider)
    preview.row_limit = 2
    assert preview.describe()["row_limit"] == 2
    rows = preview.query()
    assert rows["total_count"] is None
    assert rows["table"]["id"].to_pylist() == [1, 2]
    assert rows["table"]["__pret_row_id"].to_pylist() == [0, 1]
    provider = IbisDataFrameProvider(preview.expression, row_limit=1, paging=True)
    result = provider.query(
        {
            "filter": {"kind": "predicate", "column": "id", "operator": "gte", "value": 2},
            "sort": [{"column": "id", "direction": "desc"}],
            "limit": 1,
            "locate": {"column": "id", "values": [2, 1]},
        }
    )
    assert result["total_count"] == 2
    assert result["table"]["id"].to_pylist() == [3]
    assert result["positions"] == [1, None]
    page = provider.query({"offset": 1, "limit": 1})
    assert page["table"].column_names == ["id", "label", "__pret_row_id"]
    assert page["table"]["id"].to_pylist() == [2]
    projected = provider.query({"offset": 1, "limit": 1, "columns": ["label"]})
    assert projected["table"].column_names == ["label", "__pret_row_id"]
    assert projected["table"]["label"].to_pylist() == ["two"]
    if engine == "pyspark":
        assert backend.conf.get("spark.sql.session.timeZone") == "Europe/Paris"
        exact = DataFrame(
            backend.sql(
                "SELECT 9007199254740993L AS id, timestamp_micros(1704067200000000) AS time UNION ALL SELECT NULL, NULL"
            )
        )
        table = exact.materialize()
        assert table["id"].to_pylist() == [9007199254740993, None]
        assert table["time"].type == pa.timestamp("us", tz="Europe/Paris")
        assert table["time"].cast(pa.int64()).to_pylist() == [1704067200000000, None]
        del exact, frame, preview, provider
        gc.collect()
        assert backend.catalog.listTables() == views


def test_filters_run_on_sqlite(request):
    """
    Combined, null, date, large integer and text filters select the expected SQLite rows, only icontains ignores case
    """
    backend = ibis.sqlite.connect(":memory:")
    request.addfinalizer(backend.disconnect)
    backend.raw_sql("CREATE TABLE data(list_id INTEGER, active BOOLEAN, name TEXT, day DATE)")
    backend.raw_sql(
        "INSERT INTO data VALUES (1,1,'ABC','2026-08-11'),(2,1,'abc','2026-08-12'),(3,0,'xAbCx','2026-08-12'),"
        "(4,1,'ab','2026-08-13'),(9223372036854775807,0,NULL,NULL)"
    )
    contains_abc = {"kind": "predicate", "column": "name", "operator": "icontains", "value": "abc"}
    for node, expected in [
        (contains_abc, [1, 2, 3]),
        ({"kind": "predicate", "column": "name", "operator": "contains", "value": "abc"}, [2]),
        ({"kind": "predicate", "column": "name", "operator": "starts_with", "value": "AB"}, [1]),
        ({"kind": "predicate", "column": "name", "operator": "ends_with", "value": "Cx"}, [3]),
        ({"kind": "predicate", "column": "list_id", "operator": "gte", "value": 3}, [3, 4, 2**63 - 1]),
        (
            {
                "kind": "and",
                "children": [{"kind": "predicate", "column": "active", "operator": "eq", "value": True}, contains_abc],
            },
            [1, 2],
        ),
        ({"kind": "not", "child": {"kind": "predicate", "column": "name", "operator": "is_null"}}, [1, 2, 3, 4]),
        ({"kind": "predicate", "column": "day", "operator": "eq", "value": "2026-08-12"}, [2, 3]),
        # Browsers send 64 bit integers as strings to keep their precision
        ({"kind": "predicate", "column": "list_id", "operator": "eq", "value": "9223372036854775807"}, [2**63 - 1]),
    ]:
        rows = apply_ibis_filter(backend.table("data"), node).to_pyarrow()
        assert sorted(rows["list_id"].to_pylist()) == expected, node


@pytest.mark.parametrize(
    ("node", "message"),
    [
        ({"kind": "and", "children": []}, "requires children"),
        (
            {"kind": "predicate", "column": "missing", "operator": "eq", "value": 1},
            "Unknown source filter column",
        ),
        (
            {
                "kind": "predicate",
                "column": "list_id",
                "operator": "eq",
                "value": "3.2",
            },
            "integer",
        ),
        (
            {
                "kind": "predicate",
                "column": "list_id",
                "operator": "contains",
                "value": "3",
            },
            "string column",
        ),
        (
            {"kind": "predicate", "column": "payload", "operator": "eq", "value": []},
            "unavailable",
        ),
        ({"kind": "predicate", "column": "name", "operator": "in", "value": "a"}, "must be a list"),
    ],
)
def test_reject_invalid_or_type_incompatible_filters(node, message):
    with pytest.raises(ValueError, match=message):
        apply_ibis_filter(TABLE, node)
