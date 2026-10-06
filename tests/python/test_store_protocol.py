"""
Check browser requests on a Store, Python views and subscriptions over the JSON operation log
"""

import pickle
import sqlite3
import uuid
import weakref

import pytest

from pret import DataFrame, create_store
from pret.store import rebuild_proxy, subscribe, transact


def send(store, origin, ops=(), base=None, kind="edit", edits=(), request_id=None):
    """
    Send a browser page request to the Store, as the browser controller does
    """
    return store.doc.apply_browser_edit(
        {
            "request_id": request_id or uuid.uuid4().hex,
            "origin": origin,
            "kind": kind,
            "base_revision": store.doc.revision if base is None else base,
            "ops": list(ops),
            "edits": list(edits),
        }
    )


def test_concurrent_pushes_and_captured_views():
    """
    Two pages pushing from the same revision keep both items, and a captured view follows its item
    """
    store = create_store({"todos": [{"text": "milk"}]}, sync=True)
    item = store["todos"][0]
    base = store.revision
    first = send(store, "page-a", [{"op": "insert", "path": ["data", "todos", 0], "value": {"text": "eggs"}}], base)
    second = send(store, "page-b", [{"op": "insert", "path": ["data", "todos", 0], "value": {"text": "tea"}}], base)
    assert first["status"] == second["status"] == "applied"
    assert second["revision"] == first["revision"] + 1
    assert [todo["text"] for todo in store["todos"]] == ["eggs", "tea", "milk"]
    item["done"] = True
    assert store["todos"][2].to_py() == {"text": "milk", "done": True}
    assert store["todos"][2] is item


def test_conflicts_apply_nothing_and_last_writer_wins():
    """
    A request under a value removed by a newer commit is refused whole, same key writes keep the last one
    """
    store = create_store({"todos": {"a": 1}, "count": 0, "title": ""}, sync=True)
    base = store.revision
    del store["todos"]
    ops = [
        {"op": "set", "path": ["data", "count"], "value": 1},
        {"op": "set", "path": ["data", "todos", "a"], "value": 2},
    ]
    result = send(store, "page-a", ops, base)
    assert result["status"] == "conflict" and result["revision"] == base + 1
    assert store["count"] == 0
    base = store.revision
    send(store, "page-a", [{"op": "set", "path": ["data", "title"], "value": "a"}], base)
    send(store, "page-b", [{"op": "set", "path": ["data", "title"], "value": "b"}], base)
    assert store["title"] == "b"


def test_selective_undo_redo_and_retries():
    """
    Undo keeps the later edits of other pages, redo repeats what undo reversed, and retries apply once
    """
    store = create_store({"title": "a", "count": 0, "todos": []}, sync=True)
    edit = [
        {"op": "set", "path": ["data", "title"], "value": "b"},
        {"op": "set", "path": ["data", "count"], "value": 1},
        {"op": "insert", "path": ["data", "todos", 0], "value": "mine"},
    ]
    result = send(store, "page", edit, request_id="edit")
    assert send(store, "page", edit, request_id="edit") == result
    assert store["todos"].to_py() == ["mine"]
    assert result["can_undo"] and not result["can_redo"]
    send(store, "peer", [{"op": "set", "path": ["data", "count"], "value": 2}])
    send(store, "peer", [{"op": "insert", "path": ["data", "todos", 0], "value": "theirs"}])
    undone = send(store, "page", kind="undo")
    assert undone["status"] == "applied" and not undone["can_undo"] and undone["can_redo"]
    assert store.doc.to_py()["data"] == {"title": "a", "count": 2, "todos": ["theirs"]}
    send(store, "page", kind="redo")
    assert store.doc.to_py()["data"] == {"title": "b", "count": 2, "todos": ["theirs", "mine"]}
    assert send(store, "peer", kind="undo")["can_undo"]
    assert send(store, "other", kind="undo")["status"] == "failed"


def test_mixed_revision_and_awaited_callback_enter_page_history():
    """
    Ordinary values and table cells of one request are undone together, page callbacks join its history
    """
    store = create_store({"label": "a", "rows": DataFrame([{"id": 1, "x": 1}])}, sync=True)
    rows = store["rows"]
    edits = [{"resource_id": rows.resource_id, "base_revision": store.revision, "row_identity": 1, "values": {"x": 2}}]
    send(store, "page", [{"op": "set", "path": ["data", "label"], "value": "b"}], edits=edits)
    assert rows.materialize().to_pydict() == {"id": [1], "x": [2]}
    with transact(store, origin="page"):
        store["label"] = "c"
    send(store, "page", kind="undo")
    assert store["label"] == "b"
    send(store, "page", kind="undo")
    assert store["label"] == "a"
    assert rows.materialize().to_pydict() == {"id": [1], "x": [1]}


def test_browser_requests_are_checked():
    """
    Browser requests may only change the Store value and refer to DataFrames of this Store
    """
    store = create_store({"value": 1}, sync=True)
    revision = store.revision
    for op in (
        {"op": "set", "path": ["metadata", "owner"], "value": "page"},
        {"op": "delete", "path": ["data"]},
        {"op": "set", "path": ["data", "rows"], "value": {"__pret_dataframe_ref__": "unknown"}},
        {"op": "set", "path": ["data", "value"], "value": float("nan")},
        {"op": "splice", "path": ["data", "value"]},
    ):
        assert send(store, "page", [op])["status"] == "failed"
    assert store.revision == revision and store.to_py() == {"value": 1}


def test_views_values_and_saved_state():
    """
    Views compare by identity and marshal by path, values stay JSON values readable with plain SQLite
    """
    store = create_store({"todos": [{"text": "milk"}]}, sync=True)
    todos = store["todos"]
    assert todos is store["todos"] and len({todos, store["todos"]}) == 1
    assert weakref.ref(todos)() is todos
    assert todos.__reduce__() == (rebuild_proxy, (store.doc, ("data", "todos")))
    assert pickle.loads(pickle.dumps(todos[0].to_py())) == {"text": "milk"}
    store["pair"] = (1, 2.5)
    assert store["pair"].to_py() == [1, 2.5]
    for value in (float("nan"), 2**60, b"bytes", {1: "a"}):
        with pytest.raises((TypeError, ValueError)):
            store["bad"] = value
    assert "bad" not in store
    removed = todos[0]
    del todos[0]
    with pytest.raises(ValueError, match="removed"):
        removed["text"] = "eggs"
    assert removed.__reduce__() == (dict, ({"text": "milk"},))


def test_saved_state_is_plain_json(tmp_path):
    """
    The current values of a Store file can be read without Pret
    """
    path = tmp_path / "todos.pret"
    store = create_store({"title": "Groceries", "todos": []}, sync=path)
    store["todos"].append({"text": "milk", "done": False})
    store.doc.database.close()
    with sqlite3.connect(path) as connection:
        title, text = connection.execute(
            "SELECT json_extract(value, '$.data.title'), json_extract(value, '$.data.todos[0].text') FROM _pret_state"
        ).fetchone()
    assert (title, text) == ("Groceries", "milk")


def test_subscribe_after_commit_only():
    """
    Subscribers receive the operations under their value with relative paths, once the change is saved
    """
    store = create_store({"todos": [], "other": 0}, sync=True)
    events = []
    unsubscribe = subscribe(store["todos"], lambda ops, info: events.append((ops, info["origin"])))
    with transact(store):
        store["todos"].append("milk")
        store["other"] = 1
        assert events == []
    assert events == [([{"op": "insert", "path": [0], "value": "milk"}], store.doc.origin)]
    with pytest.raises(ValueError):
        with transact(store):
            store["todos"].append("eggs")
            raise ValueError("rejected")
    send(store, "page", [{"op": "set", "path": ["data", "todos", 0], "value": "tea"}])
    assert events[-1] == ([{"op": "set", "path": [0], "value": "tea"}], "page")
    unsubscribe()
    store["todos"].append("eggs")
    assert len(events) == 2


def test_saved_values_are_rewritten_once_the_log_grows(tmp_path):
    """
    Commits append their operations, the saved values catch up once the log exceeds a tenth of their size
    """
    from pret import load_store_snapshot

    path = tmp_path / "large.pret"
    store = create_store({"items": ["x" * 100] * 100, "count": 0}, sync=path)
    database = store.doc.database

    def saved():
        return database.connection.execute("SELECT revision FROM _pret_state").get

    first = saved()
    store["count"] = 1
    assert saved() == first
    assert database.read_state() == (store.doc.to_py(), store.revision)
    assert load_store_snapshot(path)["count"] == 1
    for count in range(2, 200):
        store["count"] = count
    assert first < saved() < store.revision
    database.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT revision, json_extract(value, '$.data.count') FROM _pret_state"
        ).fetchone() == (
            200,
            199,
        )
