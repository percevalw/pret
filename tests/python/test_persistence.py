"""
Check saved values, earlier versions and competing file writers
"""

import asyncio
import gc
import itertools
import multiprocessing
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

import apsw
import pyarrow as pa
import pytest

from pret import DataFrame, Store, create_store, load_store, load_store_snapshot, save_store
from pret.dataframe_sqlite import SQLiteDataFrameProvider
from pret.store import StoreDatabase, copy_store_value, transact, write_store


@pytest.mark.asyncio
async def test_sync_two_states_update(tmp_path):
    path = tmp_path / "state.bin"
    state1 = create_store({"value": 0}, sync=path)
    state2 = create_store({"value": 0}, sync=path)
    assert state1.doc.sync_id == state2.doc.sync_id

    await asyncio.sleep(0.01)

    state1["value"] = 1
    await asyncio.sleep(0.1)
    await asyncio.sleep(0.1)
    assert state1["value"] == 1
    assert state2["value"] == 1

    state2["value"] = 2
    await asyncio.sleep(0.5)
    assert state1["value"] == 2

    state1.doc._persistence_watcher.cancel()
    state2.doc._persistence_watcher.cancel()
    await asyncio.sleep(0)
    state1.doc._persistence_finalizer.detach()
    state2.doc._persistence_finalizer.detach()
    state1.doc._persistence_finalizer = None
    state2.doc._persistence_finalizer = None


@pytest.mark.asyncio
async def test_hydrate_from_existing_file(tmp_path):
    path = tmp_path / "persist.bin"
    state1 = create_store({"count": 0}, sync=path)
    state1["count"] = 7
    await asyncio.sleep(0.05)
    state1.doc._persistence_watcher.cancel()
    await asyncio.sleep(0)
    state1.doc._persistence_finalizer.detach()
    state1.doc._persistence_finalizer = None

    with pytest.warns(UserWarning) as warns:
        state2 = create_store({}, sync=path)

    assert "Initial data provided to create_store will be ignored" in warns[0].message.args[0]
    assert state2.doc.sync_id == state1.doc.sync_id
    await asyncio.sleep(0.05)
    assert state2["count"] == 7
    state2.doc._persistence_watcher.cancel()
    await asyncio.sleep(0)
    state2.doc._persistence_finalizer.detach()
    state2.doc._persistence_finalizer = None

    snapshot = load_store_snapshot(path)
    assert snapshot["count"] == 7


def edit_saved_store(path, column, value, request_id, ready, start, results):
    """
    Edit the saved Store in a child process while another process edits the same file
    """
    store = load_store(path)
    store["ui"][column] = value
    ready.put(request_id)
    start.wait()
    result = asyncio.run(
        store["annotations"].apply_edit(
            {
                "request_id": request_id,
                "base_revision": 0,
                "row_identity": 1,
                "column": column,
                "value": value,
            }
        )
    )
    results.put(result)


def test_save_and_load_store_preserve_ids_defaults_state_and_metadata(tmp_path):
    live = create_store(
        {
            "annotations": DataFrame([{"label": "A"}]).with_columns(done=False),
            "ui": {"row": 0},
        },
        sync=True,
    )
    resource_id = live["annotations"].resource_id

    saved = save_store(
        live,
        tmp_path / "review.pret",
        bounds={"annotations": {"max_rows": 2, "max_bytes": 100_000}},
        metadata={"recipe": "review", "format_version": 1},
    )
    reopened = load_store(tmp_path / "review.pret")

    assert isinstance(saved, Store)
    assert reopened["annotations"].resource_id == resource_id
    assert reopened["annotations"].binding_id != live["annotations"].binding_id
    assert reopened["annotations"].provider.query()["table"].select(["label", "done"]).to_pylist() == [
        {"label": "A", "done": False}
    ]
    assert reopened["ui"].to_py() == {"row": 0}
    assert reopened.metadata.to_py() == {"recipe": "review", "format_version": 1}
    assert load_store_snapshot(tmp_path / "review.pret", metadata=True) == {
        "recipe": "review",
        "format_version": 1,
    }


def test_generated_columns_become_persisted_values(tmp_path):
    values = itertools.count()
    live = create_store(
        {
            "tags": DataFrame([{"todo_id": 1}, {"todo_id": 1}]).with_columns(tag_id=lambda: f"tag-{next(values)}"),
            "empty_tags": DataFrame({"todo_id": pa.array([], type=pa.int64())}).with_columns(
                tag_id=lambda: "generated", done=False, comment=""
            ),
        },
        sync=True,
    )

    description = asyncio.run(live["tags"].describe())
    assert next(column for column in description["columns"] if column["name"] == "tag_id")["source_filter"] is False
    saved = save_store(live, tmp_path / "review.pret")
    rows = saved["tags"].provider.query()["table"].to_pylist()

    assert len({row["tag_id"] for row in rows}) == 2
    assert saved["tags"].with_columns(tag_id=lambda: "new") is saved["tags"]
    empty = saved["empty_tags"]
    assert empty.provider.schema.field("tag_id").type == pa.string()
    assert empty.provider.schema.field("done").type == pa.bool_()
    assert empty.provider.schema.field("comment").type == pa.string()
    empty.insert({"todo_id": 1, "tag_id": "inserted"})


def test_save_store_refuses_collision_and_removes_failed_publish(tmp_path):
    destination = tmp_path / "review.pret"
    live = create_store({"rows": DataFrame([{"value": 1}, {"value": 2}])}, sync=True)

    with pytest.raises(ValueError, match="row"):
        save_store(live, destination, bounds={"rows": {"max_rows": 1}})
    assert not destination.exists()

    save_store(live, destination)
    with pytest.raises(FileExistsError):
        save_store(live, destination)


def test_writer_reads_external_commits_before_its_next_edit(tmp_path):
    async def edit_both():
        path = tmp_path / "review.pret"
        first = save_store(create_store({"ui": {}}, sync=True), path)
        second = load_store(path)

        first["ui"]["left"] = "A"
        second["ui"]["right"] = "B"
        for _ in range(50):
            if first["ui"].get("right") == "B" and second["ui"].get("left") == "A":
                break
            await asyncio.sleep(0.02)
        return first["ui"].to_py(), second["ui"].to_py()

    first, second = asyncio.run(edit_both())
    assert first == {"left": "A", "right": "B"}
    assert second == {"left": "A", "right": "B"}


def test_two_processes_merge_dataframe_and_ordinary_state_edits(tmp_path):
    path = tmp_path / "review.pret"
    save_store(
        create_store(
            {
                "annotations": DataFrame([{"left": "a", "right": "b"}]),
                "ui": {},
            },
            sync=True,
        ),
        path,
    )
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    results = context.Queue()
    start = context.Event()
    processes = [
        context.Process(
            target=edit_saved_store,
            args=(path, column, value, request_id, ready, start, results),
        )
        for column, value, request_id in [
            ("left", "A", "process-left"),
            ("right", "B", "process-right"),
        ]
    ]
    for process in processes:
        process.start()
    assert {ready.get(timeout=10), ready.get(timeout=10)} == {
        "process-left",
        "process-right",
    }
    start.set()
    responses = [results.get(timeout=10), results.get(timeout=10)]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    reopened = load_store(path)
    rows = reopened["annotations"].provider.query()["table"].to_pylist()
    assert {response["status"] for response in responses} == {"applied"}
    assert rows[0]["left"] == "A"
    assert rows[0]["right"] == "B"
    assert reopened["ui"].to_py() == {"left": "A", "right": "B"}
    conflict = asyncio.run(
        reopened["annotations"].apply_edit(
            {
                "base_revision": 0,
                "row_identity": 1,
                "column": "left",
                "value": "stale",
            }
        )
    )
    assert conflict["status"] == "conflict"
    assert conflict["revision"] == reopened.revision
    assert reopened["annotations"].provider.materialize()["left"].to_pylist() == ["A"]


def test_assigning_external_views_only_materializes_when_saving(tmp_path):
    """
    Keep recipe assignments linked to their source until the Store is saved
    """
    calls = []

    class Source:
        async def describe(self):
            return {
                "columns": [{"name": "id", "type": "int64", "nullable": False}],
                "capabilities": {"edit": False, "changes": False},
            }

        async def query(self, options=None):
            return {"table": pa.table({"id": [1]})}

        def materialize(self, bounds=None):
            calls.append(bounds)
            return pa.table({"id": [1]})

    live = create_store({"todos": DataFrame(provider=Source())}, sync=True)
    live["todos"] = live["todos"].with_columns(done=False)
    live["other"] = DataFrame(provider=Source())
    assert calls == []
    rows = asyncio.run(live["todos"].call_provider("query", {}))["table"].to_pylist()
    assert rows == [{"id": 1, "done": False}]
    assert calls == []

    saved = save_store(live, tmp_path / "review.pret")
    assert len(calls) == 2
    assert saved["todos"].provider.query()["table"].select(["id", "done"]).to_pylist() == rows
    saved["added"] = DataFrame(provider=Source())
    assert len(calls) == 2
    assert isinstance(saved["added"].provider, Source)
    reopened = load_store(tmp_path / "review.pret")
    reopened["added"] = DataFrame(provider=Source())
    assert len(calls) == 2
    assert asyncio.run(reopened["added"].call_provider("query", {}))["table"].num_rows == 1


def test_sql_views_follow_store_entries_and_preserve_exact_values(tmp_path):
    path = tmp_path / "annotation.sqlite"
    frame = DataFrame(
        pa.table(
            {
                "amount": pa.array([Decimal("-10.20"), Decimal("2.30")], type=pa.decimal128(38, 2)),
                "unsigned": pa.array([2**64 - 1, 1], type=pa.uint64()),
                "instant": pa.array([-1, 1789381234567890123], type=pa.timestamp("ns")),
            }
        )
    )
    store = save_store(create_store({"todos": frame}, sync=True), path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM todos").fetchall() == [
            ("-10.20", str(2**64 - 1), "1969-12-31T23:59:59.999999999Z"),
            ("2.30", "1", "2026-09-14T10:20:34.567890123Z"),
        ]
        with store.doc.edit_transaction():
            store["alias"] = store["todos"]
            del store["todos"]
        assert connection.execute("SELECT amount FROM alias").fetchall() == [("-10.20",), ("2.30",)]
        assert connection.execute("SELECT count(*) FROM sqlite_master WHERE name='todos'").fetchone() == (0,)
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchall() == [("alias",)]
    store["ALIAS"] = store["alias"]
    # Keys differing only in case get no view, SQLite names are case insensitive
    assert not store.doc.database.connection.execute("SELECT count(*) FROM sqlite_master WHERE type='view'").get
    del store["ALIAS"]
    assert store.doc.database.connection.execute("SELECT count(*) FROM alias").get == 2
    result = store["alias"].provider.query({"sort": [{"column": "amount", "direction": "desc"}]})
    assert result["table"]["amount"].to_pylist() == [Decimal("2.30"), Decimal("-10.20")]


@pytest.mark.parametrize("with_dataframe", [False, True])
def test_worker_save_registers_browser_sync_only_after_loading(tmp_path, monkeypatch, with_dataframe):
    """
    Save captured values on a worker thread without registering browser synchronization.
    Loading the file on the notebook thread then registers synchronization and restores those values.
    """
    import pret.store

    gc.collect()

    async def copy_then_edit():
        live = create_store({"ui": {"selected": 1}}, sync=True)
        if with_dataframe:
            live["rows"] = DataFrame({"value": [1, 2]})
        captured = copy_store_value(live, materialize=False)
        live["ui"]["selected"] = 2
        notebook_thread = threading.get_ident()
        connections = []
        materializations = []
        materialize = DataFrame.materialize

        def trace_materialize(dataframe, bounds=None):
            """
            Check which thread fetches rows while saving the Store
            """
            materializations.append(threading.get_ident())
            return materialize(dataframe, bounds)

        monkeypatch.setattr(DataFrame, "materialize", trace_materialize)
        monkeypatch.setattr(
            pret.store,
            "get_manager",
            lambda: SimpleNamespace(register_state=lambda *args: connections.append(threading.get_ident())),
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(write_store, captured, tmp_path / "worker.pret", metadata={"recipe": "review"})
            while not pending.done():
                await asyncio.sleep(0.01)
            destination = pending.result()
        assert connections == []
        assert all(thread != notebook_thread for thread in materializations)
        assert bool(materializations) == with_dataframe
        saved = load_store(destination)
        assert connections == [notebook_thread]
        assert saved["ui"]["selected"] == 1
        assert saved.metadata["recipe"] == "review"
        if with_dataframe:
            assert saved["rows"].resource_id == live["rows"].resource_id
            assert saved["rows"].binding_id != live["rows"].binding_id
            assert saved["rows"].provider.materialize().to_pylist() == [{"value": 1}, {"value": 2}]
        saved["ui"]["selected"] = 3
        assert load_store_snapshot(destination)["ui"]["selected"] == 3

    asyncio.run(copy_then_edit())
    gc.collect()


def test_persisted_reopen_and_unsupported_format(tmp_path):
    """
    Retry an insertion after reopening without adding a second row and reject unsupported files
    """
    path = tmp_path / "store.sqlite"
    database = StoreDatabase(path)
    provider = database.add_dataframe(pa.table({"label": ["old"]}), "docs")
    with database.transaction("persisted"):
        provider.apply_edit({"base_revision": database.revision, "kind": "insert", "values": {"label": "new"}})
    result = database.acknowledgment("persisted")
    assert database.connection.execute("PRAGMA journal_mode").get == "persist"
    database.close()
    reopened = StoreDatabase(path)
    provider = SQLiteDataFrameProvider(reopened, "docs")
    assert reopened.acknowledgment("persisted") == result
    assert provider.materialize().to_pydict() == {"label": ["old", "new"]}
    reopened.close()
    for version in (0, 2, 5):
        unsupported = tmp_path / f"unsupported-{version}.sqlite"
        connection = apsw.Connection(str(unsupported))
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.execute(f"PRAGMA user_version={version}")
        connection.close()
        before = unsupported.read_bytes()
        with pytest.raises(ValueError, match="Unsupported"):
            StoreDatabase(unsupported)
        assert unsupported.read_bytes() == before


def test_concurrent_new_database_initialization(tmp_path):
    """
    Concurrent connections to the same new database must use the same tables
    """
    from threading import Barrier

    path = tmp_path / "shared.sqlite"
    ready = Barrier(4)

    def open_database():
        """
        Wait until all connections are ready before opening the same new database file
        """
        ready.wait()
        database = StoreDatabase(path)
        try:
            return database.revision
        finally:
            database.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(open_database) for _ in range(4)]
        assert [future.result() for future in futures] == [0, 0, 0, 0]


def test_public_save_preserves_history_and_snapshot_copies(tmp_path):
    """
    Saving a Store keeps its edit log, and a snapshot copies ordinary values and DataFrame rows
    """

    source = create_store({"reviewed": False, "rows": DataFrame({"label": ["old"]})})
    with transact(source):
        source["reviewed"] = True
        source["rows"].update(identity={"label": "old"}, values={"label": "new"})
    current_revision = source.revision
    path = tmp_path / "history.sqlite"
    saved = save_store(source, path, bounds={"rows": {"max_rows": 2}}, metadata={"recipe": "review"})
    snapshot = load_store_snapshot(path)
    try:
        assert saved.revision == current_revision + 1
        saved["rows"].update(identity={"label": "new"}, values={"label": "later"})
        assert snapshot["reviewed"] is True
        assert snapshot["rows"].materialize().to_pydict() == {"label": ["new"]}
        assert load_store_snapshot(path, metadata=True) == {"recipe": "review"}
    finally:
        saved.doc.database.close()
        source.doc.database.close()


def test_released_ordinary_file_layout_is_converted_on_open(tmp_path):
    """
    Read released Store files without changing them, and convert them to SQLite when a Store opens them
    """
    import base64

    fixture = base64.b64decode(
        "YWFhYWFhYWEtYmJiYi1jY2NjLWRkZGQtZWVlZWVlZWVlZWVlNQAAAAEECwAnAQFfAV8BKAALAAVjb3VudAF9AScACwAGbmVzdGVkASgACwIFbGFiZWwBdwNvbGQA"
    )
    path = tmp_path / "released.bin"
    path.write_bytes(fixture)
    assert load_store_snapshot(path) == {"count": 1, "nested": {"label": "old"}}
    assert path.read_bytes() == fixture
    with pytest.warns(UserWarning, match="will be ignored"):
        store = create_store({"count": 0}, sync=path)
    assert store.doc.sync_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert path.with_name("released.bin.legacy").read_bytes() == fixture
    store["nested"]["label"] = "edited"
    assert load_store_snapshot(path) == {"count": 1, "nested": {"label": "edited"}}
    reopened = load_store(path)
    assert reopened.doc.sync_id == store.doc.sync_id and reopened["count"] == 1


def test_released_files_need_the_legacy_extra(tmp_path, monkeypatch):
    """
    Store files of Pret 0.6 and earlier are converted with the legacy extra, its absence is explained
    """
    path = tmp_path / "released.bin"
    path.write_bytes(b"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    monkeypatch.setitem(sys.modules, "pycrdt", None)
    with pytest.raises(ImportError, match="pret\\[legacy\\]"):
        load_store(path)


def test_save_publication_does_not_overwrite_a_racing_destination(tmp_path, monkeypatch):
    """
    Saving must not replace a file created by another process during the save
    """
    import os

    path = tmp_path / "store.sqlite"
    link = os.link

    def publish(source, destination):
        """
        Create the destination just before save_store tries to place its completed file there
        """
        path.write_bytes(b"another writer")
        link(source, destination)

    monkeypatch.setattr(os, "link", publish)
    with pytest.raises(FileExistsError):
        save_store(create_store({"value": 1}), path)
    assert path.read_bytes() == b"another writer"


def test_discarded_persistent_store_releases_watcher(tmp_path):
    """
    Deleting the last Python Store reference must release its timer and document
    """
    import weakref

    async def check():
        """
        Start the timer before deleting the last Store reference
        """
        store = create_store({"value": 1}, sync=tmp_path / "watched.sqlite")
        reference = weakref.ref(store.doc)
        task = store.doc._persistence_watcher
        await asyncio.sleep(0)
        del store
        gc.collect()
        await asyncio.sleep(0)
        assert reference() is None
        assert task.cancelled()

    asyncio.run(check())


def test_save_nested_container_copies_the_requested_values(tmp_path):
    """
    Saving a nested mapping copies its values, while saving the whole Store also keeps its edit log
    """

    source = create_store({"nested": {"value": 1}, "other": 2})
    saved = save_store(source["nested"], tmp_path / "nested.sqlite")
    try:
        assert saved.to_py() == {"value": 1}
        assert create_store([1, 2]).revision == 1
    finally:
        saved.doc.database.close()
        source.doc.database.close()


def test_loading_pins_document_and_database_revision(tmp_path, monkeypatch):
    """
    Loading a Store must not miss an edit from the other process saved between reading its values and connecting it
    """

    path = tmp_path / "load.sqlite"
    peer = create_store({"value": 1}, sync=path)
    before = peer.revision
    initialize, read_state = StoreDatabase.__init__, StoreDatabase.read_state
    wrote = False

    def initialize_reader(database, *args, **kwargs):
        """
        Allow the test writer to save while the other connection is loading the Store
        """
        initialize(database, *args, **kwargs)
        database.connection.execute("PRAGMA journal_mode=WAL")
        peer.doc.database.connection.execute("PRAGMA journal_mode=WAL")

    def read_state_then_write(database):
        """
        Save the peer edit after the loading connection read the current values
        """
        nonlocal wrote
        state = read_state(database)
        if database is not peer.doc.database and not wrote:
            wrote = True
            peer["value"] = 2
        return state

    monkeypatch.setattr(StoreDatabase, "__init__", initialize_reader)
    monkeypatch.setattr(StoreDatabase, "read_state", read_state_then_write)
    loaded = load_store(path)
    try:
        assert wrote
        assert loaded.doc.revision == loaded.revision == before + 1
        assert loaded["value"] == 2
    finally:
        loaded.doc.database.close()
        peer.doc.database.close()
