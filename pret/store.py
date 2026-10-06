from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import shutil
import tempfile
import threading
import uuid
import warnings
import weakref
from collections.abc import MutableMapping, MutableSequence
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from itertools import islice
from pathlib import Path
from typing import Any, Optional, Union

import apsw
import pyarrow as pa

from pret import store_ops
from pret.arrow import serialize_arrow_as_bytes
from pret.dataframe_sqlite import (
    INTERNAL_ROW_ID,
    ArrowSnapshotProvider,
    SQLiteDataFrameProvider,
    decode_value,
    encode_value,
    numeric_key,
    physical_arrow_type,
    quote_identifier,
    sqlite_type,
)
from pret.manager import BinaryPayload, get_manager, server_only
from pret.marshal import marshal_as

DATAFRAME_REF_KEY = "__pret_dataframe_ref__"
# SQLite PRAGMA user_version of Store files written by this version
STORE_FORMAT = 1
SESSION_ORIGIN = ContextVar("pret_session_origin", default=None)
# Ordinary values of a new Store file, before its create commit
EMPTY_STATE = {"data": None, "metadata": {}, "id": None}


def is_marker(value):
    """
    Tell whether a Store value is the reference to a DataFrame saved in the Store database
    """
    return isinstance(value, dict) and len(value) == 1 and DATAFRAME_REF_KEY in value


class MapNode(dict):
    """
    Mapping of the live Store tree, its parent locates it for operation paths
    """

    __slots__ = ("parent", "proxy", "__weakref__")


class ListNode(list):
    """
    List of the live Store tree, its parent locates it for operation paths
    """

    __slots__ = ("parent", "proxy", "__weakref__")


def adopt(value, parent):
    """
    Turn a JSON value into nodes of the live tree under parent, existing nodes are moved back unchanged.

    store_ops.apply calls this for operation values, and for removed nodes when a rollback restores them,
    so proxies captured before the rollback still show the same values.
    """
    if isinstance(value, (MapNode, ListNode)):
        value.parent = parent
        return value
    if isinstance(value, dict) and not is_marker(value):
        node = MapNode()
        node.parent, node.proxy = parent, None
        for key, child in value.items():
            dict.__setitem__(node, key, adopt(child, node))
        return node
    if isinstance(value, list):
        node = ListNode()
        node.parent, node.proxy = parent, None
        list.extend(node, (adopt(child, node) for child in value))
        return node
    return value


def node_path(node):
    """
    Return the operation path of a node, or None once it was removed from its Store
    """
    path = []
    while node.parent is not None:
        parent = node.parent
        items = parent.items() if isinstance(parent, MapNode) else enumerate(parent)
        key = next((key for key, child in items if child is node), None)
        if key is None:
            return None
        path.append(key)
        node = parent
    return path[::-1]


def prepare_value(doc, value, name):
    """
    Return the JSON value saved for a value assigned to a Store, DataFrames become references to their table.

    Store proxies are copied, their DataFrames are attached to this Store as well.
    """
    from pret.dataframe import DataFrame

    if isinstance(value, DataFrame):
        return {DATAFRAME_REF_KEY: doc.attach_dataframe(str(name), value).resource_id}
    if isinstance(value, (StoreDict, dict)):
        if not all(isinstance(key, str) for key in value.keys()):
            raise TypeError(f"Store mapping keys must be str, got {list(value.keys())!r}")
        return {key: prepare_value(doc, value[key], key) for key in value.keys()}
    if isinstance(value, (StoreList, list, tuple)):
        return [prepare_value(doc, item, name) for item in value]
    return store_ops.validate(value)


def store_mutation(method):
    """
    Run a mapping or list edit inside one Store transaction, so several operations are saved together
    """

    @wraps(method)
    def mutate(self, *args, **kwargs):
        with self.doc.edit_transaction():
            return method(self, *args, **kwargs)

    return mutate


class Store:
    """
    Live view of a mapping or list of a Store, edits are saved as Store operations.

    Views compare by identity, the same value keeps the same view, so a view captured before an
    insertion in its parent list keeps following its value.
    """

    __slots__ = ("node", "doc", "__weakref__")
    __eq__ = object.__eq__
    __hash__ = object.__hash__

    def __init__(self, node, doc):
        self.node = node
        self.doc = doc

    def path(self):
        """
        Return the operation path of this value, refusing edits once it was removed from the Store
        """
        path = node_path(self.node)
        if path is None:
            raise ValueError("This Store value was removed from its Store")
        return path

    def edit(self, op):
        """
        Apply one operation on this value within the current Store transaction
        """
        self.doc.apply_local({**op, "path": [*self.path(), *op["path"]]})

    def wrap(self, value):
        """
        Return the view of a child node, the DataFrame of a reference, or a plain value
        """
        if isinstance(value, (MapNode, ListNode)):
            return self.doc.proxy(value)
        if is_marker(value):
            return self.doc._dataframe_registry.get(value[DATAFRAME_REF_KEY], value)
        return value

    def to_py(self):
        """
        Return a plain copy of this value, DataFrames stay references
        """
        return store_ops.clone(self.node)

    @property
    def revision(self):
        """
        Return the latest saved revision number, including edits saved by other processes
        """
        self.doc.refresh()
        return self.doc.revision

    @property
    def metadata(self):
        """
        Return the metadata saved with this Store
        """
        return self.doc.proxy(self.doc.root["metadata"])

    def __reduce__(self):
        path = node_path(self.node)
        if path is None:
            return (dict if isinstance(self.node, MapNode) else list), (self.to_py(),)
        return rebuild_proxy, (self.doc, tuple(path))

    def __repr__(self):
        return f"{type(self).__name__}({self.to_py()!r})"


class StoreDict(Store, MutableMapping):
    """
    Mapping of a Store, see Store
    """

    __slots__ = ()
    __eq__ = object.__eq__
    __hash__ = object.__hash__

    def __getitem__(self, key):
        return self.wrap(self.node[key])

    def __iter__(self):
        return iter(list(self.node))

    def __len__(self):
        return len(self.node)

    def __contains__(self, key):
        return key in self.node

    @store_mutation
    def __setitem__(self, key, value):
        if not isinstance(key, str):
            raise TypeError(f"Store mapping keys must be str, got {key!r}")
        self.edit({"op": "set", "path": [key], "value": prepare_value(self.doc, value, key)})

    @store_mutation
    def __delitem__(self, key):
        if key not in self.node:
            raise KeyError(key)
        self.edit({"op": "delete", "path": [key]})

    clear = store_mutation(MutableMapping.clear)
    pop = store_mutation(MutableMapping.pop)
    popitem = store_mutation(MutableMapping.popitem)
    setdefault = store_mutation(MutableMapping.setdefault)
    update = store_mutation(MutableMapping.update)


class StoreList(Store, MutableSequence):
    """
    List of a Store, see Store
    """

    __slots__ = ()
    __eq__ = object.__eq__
    __hash__ = object.__hash__

    def index_of(self, index, insert=False):
        """
        Return the non negative position of an index, refusing positions outside the list
        """
        size = len(self.node)
        index = index + size if index < 0 else index
        if insert:
            return min(max(index, 0), size)
        if not 0 <= index < size:
            raise IndexError("Store list index out of range")
        return index

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self.wrap(value) for value in self.node[index]]
        return self.wrap(self.node[self.index_of(index)])

    def __len__(self):
        return len(self.node)

    @store_mutation
    def __setitem__(self, index, value):
        if isinstance(index, slice):
            positions = range(len(self.node))[index]
            values = [prepare_value(self.doc, item, "dataframe") for item in value]
            if index.step not in (None, 1):
                if len(values) != len(positions):
                    raise ValueError("Extended slice assignment needs as many values as positions")
                for position, item in zip(positions, values):
                    self.edit({"op": "set", "path": [position], "value": item})
                return
            start = positions.start if len(positions) else self.index_of(index.start or 0, insert=True)
            for _ in positions:
                self.edit({"op": "delete", "path": [start]})
            for offset, item in enumerate(values):
                self.edit({"op": "insert", "path": [start + offset], "value": item})
            return
        self.edit({"op": "set", "path": [self.index_of(index)], "value": prepare_value(self.doc, value, "dataframe")})

    @store_mutation
    def __delitem__(self, index):
        if isinstance(index, slice):
            for position in sorted(range(len(self.node))[index], reverse=True):
                self.edit({"op": "delete", "path": [position]})
            return
        self.edit({"op": "delete", "path": [self.index_of(index)]})

    @store_mutation
    def insert(self, index, value):
        value = prepare_value(self.doc, value, "dataframe")
        self.edit({"op": "insert", "path": [self.index_of(index, insert=True)], "value": value})

    @store_mutation
    def move(self, start, end):
        """
        Move the item at start before the item at end, both positions taken before the move.
        The view of the moved item no longer follows it.
        """
        start, end = self.index_of(start), self.index_of(end, insert=True)
        value = store_ops.clone(self.node[start])
        self.edit({"op": "delete", "path": [start]})
        self.edit({"op": "insert", "path": [end - 1 if end > start else end], "value": value})

    append = store_mutation(MutableSequence.append)
    extend = store_mutation(MutableSequence.extend)
    pop = store_mutation(MutableSequence.pop)
    remove = store_mutation(MutableSequence.remove)
    clear = store_mutation(MutableSequence.clear)
    reverse = store_mutation(MutableSequence.reverse)


def dataframe_keys(root):
    """
    Map top-level Store keys to the resource IDs of their DataFrames, for StoreDatabase.update_views
    """
    data = root["data"]
    if not isinstance(data, dict):
        return {}
    return {key: value[DATAFRAME_REF_KEY] for key, value in data.items() if is_marker(value)}


class StoreDoc:
    """
    Keep the live values of a Store and save their edits as operations in its StoreDatabase.

    edit_transaction groups Python edits, DataFrame edits and browser requests in one SQLite transaction, its
    operations and their inverses are saved together with the table changes. apply_browser_edit moves browser
    operations over the commits they did not see, refresh loads the commits saved by other processes. Each
    browser page has an undo history of its revisions, reversed selectively by reverse_action.

    Attributes
    ----------
    root
        Live tree, a MapNode with the user value under "data", the Store "metadata" and its sync "id"
    database
        StoreDatabase saving the operations, the current values and the DataFrame tables
    revision
        Latest revision applied to root
    histories
        Undo and redo revisions per browser page origin
    recording
        Operations and inverses of the open transaction, replayed backwards if it fails
    """

    def __init__(self, sync_id):
        self.sync_id = sync_id
        self.dataframe_sync = "paged"
        self.dataframe_max_bytes = None
        self.database: StoreDatabase | None = None
        self.root = adopt(EMPTY_STATE, None)
        self.lock = threading.RLock()
        self.listeners = []
        self.subscribers = []
        self.error = None
        self.histories = {}
        self.revision = 0
        self.recording = None
        self.origin = "kernel-" + uuid.uuid4().hex
        self._dataframe_registry = {}

    def proxy(self, node):
        """
        Return the view of a node, the same view each time
        """
        if node.proxy is None:
            node.proxy = (StoreDict if isinstance(node, MapNode) else StoreList)(node, self)
        return node.proxy

    @property
    def data(self):
        return self.proxy(self.root["data"])

    def to_py(self):
        return store_ops.clone(self.root)

    def __reduce__(self):
        resources = dict(self._dataframe_registry)
        # Browser only Stores (no sync ID) keep their edits in the page
        connected = self.sync_id is not None
        commit = server_only(self.apply_browser_edit, f"store:{self.sync_id}:commit") if connected else None
        data = server_only(self.browser_data, f"store:{self.sync_id}:data") if connected else None
        with self.lock:
            # The values and their revision are read together, browsers then fetch the later commits
            state, revision = self.to_py(), self.revision
        # Browser controllers belong to this live document, not every opening of its file
        return rebuild_doc, (
            state,
            revision,
            resources,
            self.sync_id,
            self.origin,
            commit,
            data,
            self.dataframe_sync,
        )

    def browser_data(self, after_revision=None, watched=None):
        """
        Send the current values and table copies, or the commits after a revision, to a browser
        """
        with self.lock:
            self.refresh()
            return self.database.browser_data(after_revision, self.dataframe_max_bytes, watched)

    def attach_dataframe(self, name, dataframe):
        """
        Return the DataFrame this Store keeps for an assigned DataFrame.

        Arrow tables and tables of another Store are copied into this Store database,
        named after the Store key. Spark and Ibis sources stay queries against their source.
        """
        from pret.dataframe import DataFrame

        if dataframe.resource_id in self._dataframe_registry:
            return self._dataframe_registry[dataframe.resource_id]
        provider = dataframe.provider
        if isinstance(provider, ArrowSnapshotProvider) or (
            isinstance(provider, SQLiteDataFrameProvider) and provider.database is not self.database
        ):
            provider = self.database.add_dataframe(dataframe.materialize(), dataframe.resource_id, name)
            dataframe = DataFrame(provider=provider, resource_id=dataframe.resource_id)
        self.register_dataframe(dataframe)
        return dataframe

    def register_dataframe(self, dataframe):
        """
        Register a DataFrame so browser methods and Store edits can find it by resource ID
        """
        if self._dataframe_registry.get(dataframe.resource_id) is dataframe:
            return
        if self.sync_id is None and not isinstance(dataframe.provider, SQLiteDataFrameProvider):
            raise ValueError(
                "A Spark, Ibis or custom source requires create_store(..., sync=True), "
                "or collect its rows into DataFrame(records)"
            )
        dataframe.store_doc = self
        self._dataframe_registry[dataframe.resource_id] = dataframe

    def on_update(self, fn):
        """
        Call fn after each saved Store change, the server manager then notifies browsers
        """
        self.listeners.append(fn)
        doc_ref = weakref.ref(self)

        def unsubscribe():
            """
            Stop notifying without keeping an otherwise unused Store alive
            """
            doc = doc_ref()
            if doc is not None:
                doc.listeners.remove(fn)

        return unsubscribe

    def apply_local(self, op):
        """
        Apply an operation of the open transaction to the live tree, recording it and its inverse
        """
        if self.recording is None:
            raise RuntimeError("Store edits must run inside a Store transaction")
        inverse = store_ops.apply(self.root, op, make=adopt)
        self.recording["ops"].append(op)
        self.recording["inverse"].append(inverse)

    @contextmanager
    def edit_transaction(self, request_id=None, origin=None, kind="edit", target=None):
        """
        Group Python, DataFrame and browser edits so SQLite saves them together as one revision.

        If the block raises, the live values are restored by applying the recorded inverses, nothing is
        saved, and the uncommitted DataFrames are forgotten. Browser page origins, including their awaited
        Python callbacks, record the revision in the page undo history.
        """
        with self.lock:
            if self.recording is not None:
                with self.database.transaction() as action:
                    yield action
                return
            if self.error is not None:
                raise RuntimeError("Store values could not be restored after a failed edit") from self.error
            origin = origin or SESSION_ORIGIN.get() or self.origin
            recording = {"ops": [], "inverse": []}
            try:
                with self.database.transaction(request_id, origin, kind, target) as action:
                    self.refresh()
                    self.recording = recording
                    yield action
                    self.database.ops = recording["ops"]
                    self.database.inverse = recording["inverse"][::-1]
                    self.database.values = self.root
                    self.database.update_views(dataframe_keys(self.root))
            except BaseException:
                self.rollback(recording)
                raise
            finally:
                self.recording = None
            if kind == "edit" and origin != self.origin:
                history = self.histories.setdefault(origin, {"undo": [], "redo": []})
                history["undo"].append(action["revision"])
                history["redo"].clear()
            self.publish(action, recording["ops"])

    def rollback(self, recording):
        """
        Restore the values the failed transaction changed and forget the DataFrames it created
        """
        try:
            for inverse in reversed(recording["inverse"]):
                store_ops.apply(self.root, inverse, make=adopt)
            committed = {row[0] for row in self.database.connection.execute("SELECT resource_id FROM _pret_dataframes")}
            for resource_id, frame in list(self._dataframe_registry.items()):
                if (
                    isinstance(frame.provider, SQLiteDataFrameProvider)
                    and frame.provider.database is self.database
                    and resource_id not in committed
                ):
                    del self._dataframe_registry[resource_id]
        except BaseException as error:
            self.error = error
            raise

    def publish(self, action: dict, ops=()):
        """
        Record the latest applied revision, then notify browsers and Python subscribers
        """
        self.revision = action["revision"]
        for callback in tuple(self.listeners):
            callback()
        if ops and self.subscribers:
            info = {key: action.get(key) for key in ("revision", "origin", "kind", "request_id")}
            for node, callback in tuple(self.subscribers):
                path = node_path(node)
                if path is None:
                    continue
                size = len(path)
                events = [{**op, "path": op["path"][size:]} for op in ops if op["path"][:size] == path]
                if events:
                    callback(events, info)

    def refresh(self):
        """
        Apply the commits saved by other Python processes, in revision order, before the next Store operation
        """
        from pret.dataframe import DataFrame

        with self.lock:
            # Inside a transaction, the SQLite write lock already excludes commits by other processes
            if self.recording is not None:
                return
            for revision, request_id, origin, kind, target, ops in self.database.connection.execute(
                "SELECT revision,request_id,origin,kind,target,ops FROM _pret_changes "
                "WHERE revision>? ORDER BY revision",
                (self.revision,),
            ).fetchall():
                for (resource_id,) in self.database.connection.execute(
                    "SELECT resource_id FROM _pret_dataframes WHERE created_revision=?",
                    (revision,),
                ):
                    if resource_id not in self._dataframe_registry:
                        self.register_dataframe(
                            DataFrame(
                                provider=SQLiteDataFrameProvider(self.database, resource_id),
                                resource_id=resource_id,
                            )
                        )
                ops = json.loads(ops)
                for op in ops:
                    store_ops.apply(self.root, op, make=adopt)
                self.publish(
                    {
                        "status": "applied",
                        "revision": revision,
                        "request_id": request_id,
                        "origin": origin,
                        "kind": kind,
                        "target": target,
                    },
                    ops,
                )

    def browser_ops(self, ops):
        """
        Check the operations of a browser request, which may only change the user value and refer to known tables
        """

        def check(value):
            if is_marker(value):
                if value[DATAFRAME_REF_KEY] not in self._dataframe_registry:
                    raise ValueError("A browser edit refers to an unknown DataFrame")
                return value
            if isinstance(value, dict):
                return {key: check(child) for key, child in value.items()}
            if isinstance(value, list):
                return [check(child) for child in value]
            return value

        checked = []
        for op in ops:
            if op.get("op") not in ("set", "delete", "insert") or not op.get("path") or op["path"][0] != "data":
                raise ValueError("Browser edits may only change the Store value")
            if len(op["path"]) == 1 and op["op"] != "set":
                raise ValueError("Browser edits may only change the Store value")
            checked.append({**op, "value": check(store_ops.validate(op.get("value")))} if "value" in op else op)
        return checked

    def committed_ops(self, after_revision):
        """
        Return the operations committed after a revision, each with the origin of its commit
        """
        rows = self.database.connection.execute(
            "SELECT origin, ops FROM _pret_changes WHERE revision>? ORDER BY revision", (after_revision,)
        )
        return [{**op, "origin": origin} for origin, ops in rows for op in json.loads(ops)]

    def reverse_action(self, request_id, origin, kind):
        """
        Undo or redo the latest action of a browser page, called by apply_browser_edit.

        Table cells are reversed selectively by StoreDatabase.reverse, and ordinary values by the inverse
        operations of the action moved over the later commits, skipping values another page changed since.
        The reversal is itself recorded in the opposite list, for redo or a new undo.
        """
        history = self.histories.get(origin, {"undo": [], "redo": []})
        stack, opposite = (history["undo"], history["redo"]) if kind == "undo" else (history["redo"], history["undo"])
        if not stack:
            raise ValueError(f"This page has no action to {kind}")
        target = stack[-1]
        with self.edit_transaction(request_id, origin, kind, target) as action:
            inverse = self.database.connection.execute(
                "SELECT inverse FROM _pret_changes WHERE revision=?", (target,)
            ).get
            if inverse is None:
                raise KeyError(f"Unknown Store action {target}")
            later = self.committed_ops(target)
            for op in store_ops.transform_seq(json.loads(inverse), later, mode="undo", origin=origin):
                self.apply_local(op)
            self.database.reverse(target, origin)
        stack.pop()
        opposite.append(action["revision"])

    def apply_browser_edit(self, request):
        """
        Handle a browser request, returning the saved result if the same request was already handled.

        Browser operations are moved over the commits after the request base revision, an operation whose
        target a newer commit removed or replaced rejects the whole request as a conflict.
        """
        with self.lock:
            self.refresh()
            result = self.database.acknowledgment(request["request_id"])
            conflict = False
            try:
                if result is None:
                    kind = request.get("kind", "edit")
                    if kind in ("undo", "redo"):
                        self.reverse_action(request["request_id"], request["origin"], kind)
                    else:
                        with self.edit_transaction(request["request_id"], request["origin"]):
                            ops = self.browser_ops(request.get("ops", []))
                            base = request.get("base_revision", self.revision)
                            try:
                                ops = store_ops.transform_seq(ops, self.committed_ops(base))
                            except store_ops.Conflict:
                                conflict = True
                                raise
                            for op in ops:
                                self.apply_local(op)
                            for edit in request.get("edits", []):
                                provider = SQLiteDataFrameProvider(self.database, edit["resource_id"])
                                applied = provider.apply_edit(edit)
                                if applied["status"] != "applied":
                                    conflict = True
                                    raise ValueError("DataFrame edit conflicts with a newer action")
                    result = self.database.acknowledgment(request["request_id"])
            except Exception as error:
                result = self.database.acknowledgment(request["request_id"])
                if result is None:
                    result = {
                        "status": "conflict" if conflict else "failed",
                        "error": str(error),
                        "revision": self.database.revision,
                        "request_id": request["request_id"],
                        "blocked": self.error is not None,
                    }
                else:
                    self.refresh()
            history = self.histories.get(request["origin"], {})
            return {**result, "can_undo": bool(history.get("undo")), "can_redo": bool(history.get("redo"))}

    def dataframe_edit(self, resource_id, edit):
        """
        Save a DataFrame edit in its enclosing transaction or acknowledge a standalone request
        """
        nested = self.database.in_transaction
        request_id = None if nested else edit.get("request_id")
        if request_id is not None:
            # Return the saved reply of a request that was already committed
            previous = self.database.acknowledgment(request_id)
            if previous is not None:
                return previous
        result = None
        try:
            with self.edit_transaction(request_id, edit.get("origin")) as change:
                result = SQLiteDataFrameProvider(self.database, resource_id).apply_edit(edit)
                if result["status"] != "applied":
                    raise ValueError("DataFrame edit conflicts with a newer action")
                if not nested:
                    # Standalone DataFrame callers receive one flat group rather than browser edit groups
                    change.pop("row_identities", None)
                    change.update(result)
        except Exception:
            previous = self.database.acknowledgment(request_id) if request_id is not None else None
            if previous is not None:
                return previous
            if result is None or result["status"] != "conflict":
                raise
            result["revision"] = self.revision
        return result

    async def watch(self):
        """
        Check periodically for edits saved by other processes while this Store is still in use
        """
        doc_ref = weakref.ref(self)
        del self
        while True:
            await asyncio.sleep(0.2)
            doc = doc_ref()
            if doc is None:
                return
            doc.refresh()
            del doc


@contextmanager
def write_transaction(connection):
    """
    Take the SQLite write lock before checking table versions and undo SQL changes if the block raises
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        if not connection.get_autocommit():
            connection.execute("ROLLBACK")
        raise


class StoreDatabase:
    """
    Store the values and edit log of one Pret Store in SQLite.

    Each _pret_changes row holds the operations of one revision on the ordinary values, their inverses and the
    SQL changes of its DataFrame tables. A transaction saves them together with one revision number and request
    ID. _pret_state holds the ordinary values as JSON at its revision, it is rewritten once the operations logged
    since exceed a tenth of its size and when the database closes, read_state replays the operations after it.
    The log records which cells changed and who changed them, so undo can preserve another user's edits and a
    repeated browser request can return its earlier result.

    build_store attaches this database to StoreDoc, which sends updates to browsers after saving.
    SQLiteDataFrameProvider fetches and edits the table rows. DataFrame queries can also use an in memory
    database without a live document.

    Parameters
    ----------
    path
        File to open, or :memory: for a Store that is not saved to disk
    readonly
        Open an existing file without writing to it, used by load_store_snapshot

    Attributes
    ----------
    ops, inverse
        Operations of the current transaction and their inverses, in the order they undo, set by StoreDoc
    values
        Live values after the current transaction, set by StoreDoc so _pret_state is rewritten without replaying
    table_changes
        APSW session recording SQL row changes for undo, closed when the transaction ends
    """

    def __init__(self, path=":memory:", readonly=False):
        self.path = str(path)
        self.readonly = readonly
        self.action = None
        self.table_changes: apsw.Session | None = None
        self.failure = None
        self.ops: list = []
        self.inverse: list = []
        self.values = None
        flags = apsw.SQLITE_OPEN_READONLY if readonly else apsw.SQLITE_OPEN_READWRITE | apsw.SQLITE_OPEN_CREATE
        self.connection = apsw.Connection(self.path, flags=flags)
        self.connection.set_busy_timeout(5000)
        self.connection.create_scalar_function("_pret_numeric_key", numeric_key, 1, deterministic=True)
        version = self.connection.execute("PRAGMA user_version").get
        # An empty writable file becomes a Store, files with other tables or formats are refused
        empty = (
            version == 0
            and not readonly
            and not self.connection.execute("SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").get
        )
        if version != STORE_FORMAT and not empty:
            self.connection.close()
            raise ValueError(f"Unsupported PRET database format {version}")
        if not readonly:
            if self.path != ":memory:":
                self.connection.execute("PRAGMA journal_mode=PERSIST")
                self.connection.execute("PRAGMA synchronous=FULL")
            if empty:
                with write_transaction(self.connection):
                    # Another process may have created the Store while this one waited for the lock
                    version = self.connection.execute("PRAGMA user_version").get
                    if version == STORE_FORMAT:
                        return
                    if version != 0:
                        raise ValueError(f"Unsupported PRET database format {version}")
                    self.connection.execute(f"""
                        PRAGMA user_version={STORE_FORMAT};
                        CREATE TABLE _pret_state(
                          id INTEGER PRIMARY KEY CHECK(id=1),
                          revision INTEGER NOT NULL,
                          value TEXT NOT NULL
                        );
                        CREATE TABLE _pret_dataframes(
                          resource_id TEXT PRIMARY KEY,
                          table_name TEXT NOT NULL UNIQUE,
                          arrow_schema BLOB NOT NULL,
                          created_revision INTEGER NOT NULL
                        );
                        CREATE TABLE _pret_changes(
                          revision INTEGER PRIMARY KEY AUTOINCREMENT,
                          request_id TEXT NOT NULL UNIQUE,
                          origin TEXT NOT NULL,
                          kind TEXT NOT NULL,
                          target INTEGER,
                          ops TEXT NOT NULL,
                          inverse TEXT NOT NULL,
                          changeset BLOB NOT NULL
                        );
                        CREATE TABLE _pret_acknowledgments(
                          request_id TEXT PRIMARY KEY,
                          revision INTEGER NOT NULL UNIQUE,
                          status TEXT NOT NULL,
                          row_ids TEXT
                        );
                        CREATE TABLE _pret_revision_rows(
                          resource_id TEXT NOT NULL,
                          revision INTEGER NOT NULL,
                          row_identity INTEGER NOT NULL,
                          column_name TEXT NOT NULL,
                          kind TEXT NOT NULL
                        );
                        CREATE INDEX _pret_revision_rows_resource
                          ON _pret_revision_rows(resource_id, row_identity, revision);
                    """)
                    self.connection.execute(
                        "INSERT INTO _pret_state VALUES(1,0,?)", (json.dumps(EMPTY_STATE, separators=(",", ":")),)
                    )

    def update_views(self, keys):
        """
        Keep one SQL view per top-level Store DataFrame, named after its key, for plain SQLite readers.

        keys maps each top-level Store key to its DataFrame resource ID. A key that clashes with another
        SQLite object, a reserved prefix or another key in a different case gets no view.
        """
        tables = dict(self.connection.execute("SELECT resource_id,table_name FROM _pret_dataframes"))
        # Views reading a DataFrame table are PRET views, other views and tables are left alone
        sources = tuple(f"FROM {quote_identifier(table)}" for table in tables.values())
        views = {
            name: sql
            for name, sql in self.connection.execute("SELECT name,sql FROM sqlite_master WHERE type='view'")
            if sql.endswith(sources)
        }
        taken = {name.lower() for (name,) in self.connection.execute("SELECT name FROM sqlite_master")} - {
            name.lower() for name in views
        }
        lowered = [key.lower() for key in keys]
        desired = {}
        for key, resource_id in keys.items():
            name = key.lower()
            if resource_id not in tables or not key or "\x00" in key or name in taken or lowered.count(name) > 1:
                continue
            if name.startswith(("sqlite_", "_pret_")):
                continue
            table = quote_identifier(tables[resource_id])
            columns = [row[1] for row in self.connection.execute(f"PRAGMA table_info({table})")]
            selected = ",".join(quote_identifier(column) for column in columns if column != INTERNAL_ROW_ID)
            desired[key] = f"CREATE VIEW {quote_identifier(key)} AS SELECT {selected or '*'} FROM {table}"
        for name, sql in views.items():
            if desired.get(name) != sql:
                self.connection.execute(f"DROP VIEW {quote_identifier(name)}")
        for name, sql in desired.items():
            if views.get(name) != sql:
                self.connection.execute(sql)

    @property
    def revision(self):
        return self.connection.execute("SELECT coalesce(max(revision),0) FROM _pret_changes").get

    @property
    def in_transaction(self):
        return self.action is not None

    def read_state(self):
        """
        Return the current ordinary values and their revision, _pret_state with the operations logged after it
        """
        with self.connection:
            value, revision = self.connection.execute("SELECT value,revision FROM _pret_state").fetchone()
            state = json.loads(value)
            for revision, ops in self.connection.execute(
                "SELECT revision,ops FROM _pret_changes WHERE revision>? ORDER BY revision", (revision,)
            ).fetchall():
                for op in json.loads(ops):
                    store_ops.apply(state, op)
            return state, revision

    def write_state(self, values=None):
        """
        Rewrite _pret_state at the latest revision, from the live values when given or by replaying the log
        """
        if values is None:
            values, revision = self.read_state()
        else:
            revision = self.revision
        self.connection.execute(
            "UPDATE _pret_state SET revision=?, value=?", (revision, json.dumps(values, separators=(",", ":")))
        )

    @contextmanager
    def transaction(self, request_id=None, origin=None, kind="edit", target=None):
        """
        Save operations, table edits and one acknowledgement in one SQL transaction.

        Nested calls join the outer transaction. If a block raises, none of its SQL changes are saved, StoreDoc
        restores the live values.
        """
        if self.in_transaction:
            try:
                yield self.action
            except BaseException as error:
                self.failure = error
                raise
            return
        try:
            with write_transaction(self.connection):
                request_id = request_id or uuid.uuid4().hex
                if self.acknowledgment(request_id) is not None:
                    raise ValueError("Request already committed, read acknowledgment before retrying")
                self.action = {
                    "status": "applied",
                    "request_id": request_id,
                    "revision": self.revision + 1,
                    "origin": str(origin or "python"),
                    "kind": kind,
                    "target": target,
                }
                self.ops, self.inverse = [], []
                self.failure = None
                self.table_changes = apsw.Session(self.connection, "main")
                for (table,) in self.connection.execute("SELECT table_name FROM _pret_dataframes"):
                    self.table_changes.attach(table)
                yield self.action
                if self.failure is not None:
                    raise RuntimeError("A nested Store action failed") from self.failure
                result = self.action
                self.connection.execute(
                    "INSERT INTO _pret_changes VALUES(?,?,?,?,?,?,?,?)",
                    (
                        result["revision"],
                        request_id,
                        result["origin"],
                        kind,
                        target,
                        json.dumps(self.ops, separators=(",", ":")),
                        json.dumps(self.inverse, separators=(",", ":")),
                        self.table_changes.changeset(),
                    ),
                )
                row_ids = result.get("row_identities", result.get("row_identity"))
                self.connection.execute(
                    "INSERT INTO _pret_acknowledgments VALUES(?,?,?,?)",
                    (
                        request_id,
                        result["revision"],
                        result["status"],
                        json.dumps(row_ids) if row_ids is not None else None,
                    ),
                )
                # Rewriting every value on each commit would cost as much as the whole Store, edits only append
                # octet_length reads the stored size, length would read every character of the values
                logged, size = self.connection.execute(
                    "SELECT (SELECT coalesce(sum(octet_length(ops)),0) FROM _pret_changes WHERE revision>s.revision), "
                    "octet_length(s.value) FROM _pret_state s"
                ).fetchone()
                if logged > size // 10:
                    self.write_state(self.values)
        finally:
            self.action = None
            self.ops, self.inverse, self.values = [], [], None
            if self.table_changes is not None:
                self.table_changes.close()
                self.table_changes = None

    def browser_data(self, after_revision, max_bytes, watched=None):
        """
        Send Arrow tables, or the operations and row changes of the commits after a revision.

        Browsers call this once without a revision, receiving the current values and, for complete copies,
        every table, then with the last applied revision. Byte offsets identify Arrow streams in the response
        buffer. Newly created tables are copied at the final revision, so their earlier row changes are
        excluded from this response. With watched resource IDs and row IDs, only subscribed row values are
        sent, alongside summaries for query maintenance.
        """
        buffers, tables, commits = [], [], []
        watched = None if watched is None else {key: {int(row_id) for row_id in ids} for key, ids in watched.items()}
        size = 0

        def append_table(schema, rows, limit=False):
            """
            Encode SQL values using their Arrow types and return the response byte range
            """
            nonlocal size
            arrays = [
                pa.array([decode_value(row[idx], field.type) for row in rows], type=field.type)
                for idx, field in enumerate(schema)
            ]
            data = serialize_arrow_as_bytes(pa.Table.from_arrays(arrays, schema=schema))
            part = {"offset": size, "length": len(data)}
            size += len(data)
            if limit and max_bytes is not None and size > max_bytes:
                raise ValueError(f"DataFrame download exceeds dataframe_max_bytes of {max_bytes}")
            buffers.append(data)
            return part

        self.connection.execute("BEGIN")
        try:
            revision = self.revision
            if after_revision is not None and not 0 <= after_revision <= revision:
                raise ValueError("Browser revision is outside the Store history")
            resources = {}
            copied = set()
            for resource_id, name, schema, created in self.connection.execute(
                "SELECT resource_id,table_name,arrow_schema,created_revision FROM _pret_dataframes"
            ):
                schema = pa.schema(
                    [
                        pa.field(INTERNAL_ROW_ID, pa.int64(), False),
                        *pa.ipc.read_schema(pa.py_buffer(schema)),
                    ]
                )
                resources[name] = (resource_id, schema)
                if watched is not None or (after_revision is not None and created <= after_revision):
                    continue
                copied.add(resource_id)
                parts = []
                cursor = iter(
                    self.connection.execute(
                        f"SELECT * FROM {quote_identifier(name)} ORDER BY {quote_identifier(INTERNAL_ROW_ID)}"
                    )
                )
                while True:
                    rows = list(islice(cursor, 1024))
                    if not rows and parts:
                        break
                    parts.append(append_table(schema, rows, limit=True))
                    if len(rows) < 1024:
                        break
                tables.append({"resource_id": resource_id, "parts": parts})
            if after_revision is not None:
                for rev, request_id, origin, kind, target, ops, _, changeset in self.connection.execute(
                    "SELECT * FROM _pret_changes WHERE revision>? AND revision<=? ORDER BY revision",
                    (after_revision, revision),
                ):
                    groups, summaries = {}, {}
                    for change in apsw.Changeset.iter(changeset):
                        resource_id, schema = resources[change.name]
                        if resource_id in copied:
                            continue
                        values = change.old if change.op == "DELETE" else change.new
                        columns = (
                            [0]
                            if change.op == "DELETE"
                            else [idx for idx, value in enumerate(values) if value is not apsw.no_change]
                        )
                        if 0 not in columns:
                            columns.insert(0, 0)
                        row = [
                            change.old[0] if idx == 0 and values[0] is apsw.no_change else values[idx]
                            for idx in columns
                        ]
                        kind_name = change.op.lower()
                        summary = summaries.setdefault((resource_id, kind_name), {"columns": set(), "count": 0})
                        summary["columns"].update(schema[idx].name for idx in columns if idx != 0)
                        summary["count"] += 1
                        if watched is None or row[0] in watched.get(resource_id, ()):
                            groups.setdefault((change.name, kind_name, tuple(columns)), []).append(row)
                    changes = []
                    for (name, kind_name, columns), rows in groups.items():
                        resource_id, schema = resources[name]
                        schema = pa.schema([schema[idx] for idx in columns])
                        changes.append(
                            {
                                "resource_id": resource_id,
                                "kind": kind_name,
                                **append_table(schema, rows),
                            }
                        )
                    commits.append(
                        {
                            "revision": rev,
                            "request_id": request_id,
                            "origin": origin,
                            "kind": kind,
                            "target": target,
                            "ops": json.loads(ops),
                            "changes": changes,
                            "summaries": [
                                {
                                    "resource_id": resource_id,
                                    "kind": kind_name,
                                    "columns": sorted(summary["columns"]),
                                    "count": summary["count"],
                                }
                                for (resource_id, kind_name), summary in summaries.items()
                            ],
                        }
                    )
            return BinaryPayload(
                {
                    "revision": revision,
                    "tables": tables,
                    "commits": commits,
                    "state": self.read_state()[0] if after_revision is None else None,
                    "byte_count": size,
                },
                [b"".join(buffers)],
            )
        finally:
            self.connection.execute("ROLLBACK")

    def acknowledgment(self, request_id):
        """
        Reconstruct the transaction reply so browser retries do not repeat committed edits.

        Row identities are a scalar for one row, a list for a DataFrame batch or
        grouped lists for a browser transaction that edits several DataFrames.
        """
        row = self.connection.execute(
            "SELECT revision,status,row_ids FROM _pret_acknowledgments WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        revision, status, row_ids = row
        result = {"request_id": request_id, "revision": revision, "status": status}
        if row_ids is not None:
            identities = json.loads(row_ids)
            result["row_identities" if isinstance(identities, list) else "row_identity"] = identities
        return result

    def add_dataframe(self, table: pa.Table | pa.RecordBatchReader, resource_id, suggested_name="dataframe"):
        """
        Copy Arrow batches into a new SQL table and return the provider used by DataFrame queries
        """
        if INTERNAL_ROW_ID in table.schema.names or len(set(table.schema.names)) != len(table.schema.names):
            raise ValueError("DataFrame columns must be unique and exclude the internal row identity")
        with self.transaction():
            if self.connection.execute("SELECT 1 FROM _pret_dataframes WHERE resource_id=?", (resource_id,)).get:
                raise ValueError(f"DataFrame resource already exists {resource_id}")
            name = re.sub(r"[^A-Za-z0-9_]", "_", suggested_name).strip("_") or "dataframe"
            table_name = f"data_{name}_{uuid.uuid4().hex[:12]}"
            columns = "".join(
                f", {quote_identifier(field.name)} {sqlite_type(field.type)}"
                + (" NOT NULL" if not field.nullable else "")
                for field in table.schema
            )
            self.connection.execute(
                f"CREATE TABLE {quote_identifier(table_name)} "
                f"({quote_identifier(INTERNAL_ROW_ID)} INTEGER PRIMARY KEY AUTOINCREMENT{columns})"
            )
            names = table.schema.names
            sql = (
                (
                    f"INSERT INTO {quote_identifier(table_name)} ({', '.join(map(quote_identifier, names))}) "
                    f"VALUES ({','.join('?' for _ in names)})"
                )
                if names
                else f"INSERT INTO {quote_identifier(table_name)} DEFAULT VALUES"
            )
            # Undoing table creation removes the table, so there is no need to record each initial row
            batches = table if isinstance(table, pa.RecordBatchReader) else table.to_batches(max_chunksize=1024)
            for source in batches:
                for offset in range(0, source.num_rows, 1024):
                    batch = source.slice(offset, 1024)
                    values = []
                    for field, column in zip(table.schema, batch.columns):
                        column = column.cast(physical_arrow_type(field.type))
                        values.append([encode_value(value, field.type) for value in column.to_pylist()])
                    self.connection.executemany(sql, zip(*values) if values else (() for _ in range(batch.num_rows)))
            self.connection.execute(
                "INSERT INTO _pret_dataframes(resource_id,table_name,arrow_schema,created_revision) VALUES(?,?,?,?)",
                (resource_id, table_name, table.schema.serialize().to_pybytes(), self.action["revision"]),
            )
            self.table_changes.attach(table_name)
        return SQLiteDataFrameProvider(self, resource_id)

    def reverse(self, target_revision, origin):
        """
        Undo table edits for one browser action while keeping later edits by other users.

        StoreDoc.reverse_action calls this for undo and redo. Only cells and rows actually changed
        by this call are recorded, so redo cannot reapply an edit that undo skipped.
        """

        with self.transaction(origin=origin, kind="undo", target=target_revision) as result:
            row = self.connection.execute(
                "SELECT changeset FROM _pret_changes WHERE revision=?",
                (target_revision,),
            ).get
            if row is None:
                raise KeyError(f"Unknown Store action {target_revision}")
            # Resource ID and SQL column names of each DataFrame table, row identity first
            tables = {
                table_name: (resource_id, [INTERNAL_ROW_ID, *pa.ipc.read_schema(pa.py_buffer(schema)).names])
                for table_name, resource_id, schema in self.connection.execute(
                    "SELECT table_name,resource_id,arrow_schema FROM _pret_dataframes"
                )
            }
            for change in apsw.Changeset.iter(apsw.Changeset.invert(row)):
                resource_id, names = tables[change.name]
                row_id = (change.new if change.op == "INSERT" else change.old)[0]
                later = list(
                    self.connection.execute(
                        "SELECT column_name,_pret_revision_rows.kind FROM _pret_revision_rows "
                        "JOIN _pret_changes USING(revision) "
                        "WHERE resource_id=? AND row_identity=? AND revision>? AND origin!=?",
                        (resource_id, row_id, target_revision, str(origin)),
                    )
                )
                table = quote_identifier(change.name)
                identity = quote_identifier(INTERNAL_ROW_ID)
                current = self.connection.execute(f"SELECT * FROM {table} WHERE {identity}=?", (row_id,)).fetchone()
                columns = []
                row_changed = False
                if change.op == "UPDATE" and current is not None:
                    blocked = {column for recorded, _ in later for column in json.loads(recorded)}
                    if any(kind != "update" for _, kind in later):
                        continue
                    for idx, name in enumerate(names[1:], 1):
                        if change.new[idx] is apsw.no_change or name in blocked or current[idx] != change.old[idx]:
                            continue
                        self.connection.execute(
                            f"UPDATE {table} SET {quote_identifier(name)}=? WHERE {identity}=?",
                            (change.new[idx], row_id),
                        )
                        columns.append(name)
                elif change.op == "DELETE" and not later and current == change.old:
                    self.connection.execute(f"DELETE FROM {table} WHERE {identity}=?", (row_id,))
                    columns = names[1:]
                    row_changed = True
                elif change.op == "INSERT" and not later and current is None:
                    self.connection.execute(
                        f"INSERT INTO {table} VALUES({','.join('?' for _ in names)})",
                        change.new,
                    )
                    columns = names[1:]
                    row_changed = True
                if columns or row_changed:
                    self.connection.execute(
                        "INSERT INTO _pret_revision_rows VALUES(?,?,?,?,?)",
                        (
                            resource_id,
                            result["revision"],
                            row_id,
                            json.dumps(columns),
                            change.op.lower(),
                        ),
                    )
        return result

    def close(self):
        """
        Bring _pret_state up to date for plain SQLite readers, then close the connection
        """
        if (
            not self.readonly
            and self.connection.execute(
                "SELECT 1 FROM _pret_changes WHERE revision>(SELECT revision FROM _pret_state) LIMIT 1"
            ).get
        ):
            with write_transaction(self.connection):
                self.write_state()
        self.connection.close()


@marshal_as(
    js="""
return (function rebuild_proxy(doc, path) {
    return doc.proxyAt(path);
});""",
)
def rebuild_proxy(doc, path):
    """
    Return the view at a path of a Store, the browser loader rebuilds Store views this way
    """
    node = doc.root
    for part in path:
        node = node[part]
    return doc.proxy(node)


@marshal_as(
    js="""
return (function rebuild_doc(state, revision, resources, sync_id, binding_id, commit, data, dataframe_sync) {
    const manager = get_manager();
    const docs = window.__pret_store_docs__ || (window.__pret_store_docs__ = new Map());
    const existing = docs.get(binding_id);
    const doc = existing || window.storeLib.createStoreDoc(state, revision);
    doc.__pret_resources__ = Object.assign(doc.__pret_resources__ || {}, resources);
    if (binding_id) docs.set(binding_id, doc);
    doc.__pret_binding_id__ = binding_id;
    const controller = window.storeLib.attachStore(
        doc, doc.__pret_resources__, commit, sync_id ? manager.uid : window.storeLib.randomUUID(),
        data, dataframe_sync
    );
    if (sync_id && !existing) {
        manager.register_state(sync_id, doc);
        // Edits refused while disconnected are counted for use_connection_status
        controller.onRejectedWrite = (reason) => manager.notify_state_write_rejected(sync_id, reason);
        const updateConnection = () => controller.setConnected(manager.get_connection_status().connected !== false);
        manager.subscribe_connection_status(updateConnection);
        updateConnection();
    }
    return doc;
});""",
    globals={
        "get_manager": get_manager,
    },
)
def rebuild_doc(*args):
    """
    Called by the browser loader to rebuild a Store document, the Python body never runs
    """
    raise NotImplementedError("Store documents are rebuilt by the browser loader only")


def create_store(
    data,
    *,
    sync: Optional[Union[bool, str, os.PathLike]] = None,
    sync_id=None,
    dataframe_sync="paged",
    dataframe_max_bytes=None,
):
    """
    Create a new store that can be used to store and synchronize data
    between various components of the app, between a server and its clients,
    or between different processes (or across multiple runs of the app) using
    a file.

    Parameters
    ----------
    data: Any
        Initial data to store in the document. It can be a dictionary, list, or any
        simple value.
        If a callable is provided, it will be called to get the initial data. This can be
        useful if the initial data is expensive to create, and you only want to create it if
        the store is actually being initialized with it (e.g. when using file synchronization
        and the file doesn't already exist).
    dataframe_sync
        Paged queries by default, or full to copy every SQLite table to connected browsers
    dataframe_max_bytes
        Required positive Arrow byte limit for the initial full copy download
    sync: Union[bool, str, os.PathLike]
        There are three options for this parameter:

        - If false, this store will not be synchronized between a server and its
          clients. This can be useful for local-only stores, like style management.
        - If true, the store will be synchronized between a server and its clients.
          Any changes made to the store will be sent to the server and vice versa.
        - If a path is provided, the store will be synchronized with the file at this
          path. Any changes made to the store will be written to the file, and any
          changes made to the file will be read into the store. This can be useful
          if you want to persist the store to disk or share it between different
          processes (think servers or kernels).
    """
    return build_store(
        data,
        sync=sync,
        sync_id=sync_id,
        dataframe_sync=dataframe_sync,
        dataframe_max_bytes=dataframe_max_bytes,
    )


def build_store(
    data,
    *,
    sync=None,
    sync_id=None,
    database=None,
    connect=True,
    dataframe_sync="paged",
    dataframe_max_bytes=None,
):
    """
    Load the Store values and DataFrame tables of a database, creating them on a new database
    """
    from pret.dataframe import DataFrame

    if dataframe_sync not in {"paged", "full"}:
        raise ValueError("dataframe_sync must be paged or full")
    if dataframe_sync == "full" and (
        not (sync or sync_id) or type(dataframe_max_bytes) is not int or dataframe_max_bytes <= 0
    ):
        raise ValueError("Full DataFrame copies require a connected Store and a positive dataframe_max_bytes")
    persistent = isinstance(sync, (str, os.PathLike))
    path = Path(sync).expanduser() if persistent else None
    if sync and sync_id is None:
        sync_id = str(uuid.uuid4())
    doc = StoreDoc(sync_id=sync_id)
    doc.dataframe_sync = dataframe_sync
    doc.dataframe_max_bytes = dataframe_max_bytes
    if database is None:
        try:
            database = StoreDatabase(str(path) if persistent else ":memory:")
        except apsw.NotADBError:
            convert_legacy_file(path)
            database = StoreDatabase(str(path))
    doc.database = database

    # Read the values and their revision together so edits arriving during loading are not missed
    state, doc.revision = database.read_state()
    doc.root = adopt(state, None)
    for (resource_id,) in database.connection.execute(
        "SELECT resource_id FROM _pret_dataframes WHERE created_revision<=?", (doc.revision,)
    ):
        doc.register_dataframe(
            DataFrame(provider=SQLiteDataFrameProvider(database, resource_id), resource_id=resource_id)
        )
    if database.revision:
        if data is not None and not callable(data):
            warnings.warn(
                "Initial data provided to create_store will be ignored because the file already exists",
                UserWarning,
                stacklevel=3,
            )
        doc.refresh()
    else:
        with doc.edit_transaction(kind="create"):
            # Another process may have created the Store while this one waited for the lock
            if doc.revision == 0:
                value = data() if callable(data) else data
                doc.apply_local({"op": "set", "path": ["data"], "value": prepare_value(doc, value, "dataframe")})
                doc.apply_local({"op": "set", "path": ["id"], "value": sync_id or str(uuid.uuid4())})
    if sync_id is not None:
        doc.sync_id = doc.root["id"]

    if connect and doc.sync_id is not None:
        get_manager().register_state(doc.sync_id, doc)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and persistent:
            # The watcher task stops when the Store is garbage collected
            weakref.finalize(doc, loop.create_task(doc.watch()).cancel)
    return doc.data if isinstance(doc.root["data"], (MapNode, ListNode)) else doc.root["data"]


def read_legacy_values(path):
    """
    Return the sync ID, value and metadata of a released append format file, decoded with pycrdt.

    The file starts with a 36 character sync ID followed by Yjs updates, each prefixed
    by its 4 byte little endian size. A truncated last record is ignored.
    """
    try:
        from pycrdt import Doc, Map
    except ImportError:
        raise ImportError(
            f"{path} was saved by an earlier Pret release, install pret[legacy] to convert it: "
            "pip install 'pret[legacy]'"
        ) from None
    data = Path(path).read_bytes()
    doc = Doc()
    root = doc.get("_", type=Map)
    metadata = doc.get("_pret_metadata", type=Map)
    offset = 36
    while offset + 4 <= len(data):
        size = int.from_bytes(data[offset : offset + 4], "little")
        if offset + 4 + size > len(data):
            break
        doc.apply_update(data[offset + 4 : offset + 4 + size])
        offset += 4 + size
    return data[:36].decode(), root.to_py().get("_"), metadata.to_py()


def convert_legacy_file(path):
    """
    Replace a released append format file by a SQLite Store file with the same values and sync ID.

    build_store calls this once when opening such a file. The original file is kept
    next to it with a .legacy suffix, processes still using it no longer share edits.
    """
    path = Path(path)
    with path.open("rb") as file:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        # Another process may have converted the file while this one waited for the lock
        if file.read(16) == b"SQLite format 3\x00":
            return
        sync_id, value, metadata = read_legacy_values(path)
        temporary = path.with_name(f".{path.name}.converting")
        database = StoreDatabase(temporary)
        try:
            with database.transaction(kind="create"):
                database.ops = [
                    {"op": "set", "path": ["data"], "value": store_ops.validate(value)},
                    {"op": "set", "path": ["metadata"], "value": store_ops.validate(metadata or {})},
                    {"op": "set", "path": ["id"], "value": sync_id},
                ]
                database.inverse = [
                    {"op": "set", "path": [key], "value": EMPTY_STATE[key]} for key in ("id", "metadata", "data")
                ]
        finally:
            database.close()
        os.replace(path, path.with_name(path.name + ".legacy"))
        os.replace(temporary, path)


def copy_store_value(value, bounds=None, path=()):
    """
    Copy Store values and the rows of their DataFrames before write_store saves them to a new file
    """
    from pret.dataframe import DataFrame

    if isinstance(value, DataFrame):
        table = value.materialize((bounds or {}).get(".".join(path), {}))
        return DataFrame(table, resource_id=value.resource_id)
    if isinstance(value, (StoreDict, dict)):
        return {key: copy_store_value(value[key], bounds, (*path, str(key))) for key in value}
    if isinstance(value, (StoreList, list)):
        return [copy_store_value(item, bounds, (*path, str(idx))) for idx, item in enumerate(value)]
    return value


def save_store(store, path, *, bounds=None, metadata=None):
    """
    Save this Store to a new file and return a Store connected to that file
    """
    if not isinstance(store, Store):
        raise TypeError("save_store expects a PRET Store")
    return load_store(write_store(store, path, bounds=bounds, metadata=metadata))


def write_store(data, path, *, bounds=None, metadata=None):
    """
    Write Store values to a new file and return its path, called by save_store.

    Saving a whole Store copies its database with the edit log, other values are copied by copy_store_value.
    """
    from pret.dataframe import DataFrame

    destination = Path(path).expanduser()
    if destination.exists():
        raise FileExistsError(f"Store already exists at {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    temporary = directory / "store.sqlite"
    database = None
    try:
        database = StoreDatabase(temporary)
        source = data.doc if isinstance(data, Store) and data.node is data.doc.root["data"] else None
        if (
            source is not None
            and source.database is not None
            and all(
                isinstance(frame.provider, SQLiteDataFrameProvider) and frame.provider.database is source.database
                for frame in source._dataframe_registry.values()
            )
        ):
            if source.database.in_transaction:
                raise ValueError("Save requires a committed Store transaction")
            with database.connection.backup("main", source.database.connection, "main") as backup:
                while not backup.done:
                    backup.step(1024)
            store = build_store(None, sync=True, database=database, connect=False)
            pending = [(store, ())] if bounds else []
            while pending:
                value, keys = pending.pop()
                if isinstance(value, DataFrame):
                    if ".".join(keys) in bounds:
                        value.materialize(bounds[".".join(keys)])
                elif isinstance(value, StoreDict):
                    pending.extend((value[key], (*keys, str(key))) for key in value)
                elif isinstance(value, StoreList):
                    pending.extend((value[idx], (*keys, str(idx))) for idx in range(len(value)))
        else:
            store = build_store(copy_store_value(data, bounds), sync=temporary, database=database, connect=False)
        with store.doc.edit_transaction(kind="save"):
            store.doc.apply_local({"op": "set", "path": ["id"], "value": str(uuid.uuid4())})
            if metadata is not None:
                store.doc.apply_local({"op": "set", "path": ["metadata"], "value": store_ops.validate(metadata)})
        database.close()
        database = None
        os.link(temporary, destination)
    finally:
        if database is not None:
            database.close()
        shutil.rmtree(directory)
    return destination


def load_store(path, *, dataframe_sync="paged", dataframe_max_bytes=None):
    """
    Open a saved Store, changes are saved back to the same file

    Parameters
    ----------
    dataframe_sync
        Paged backend queries or full browser copies of the Store tables
    dataframe_max_bytes
        Required positive Arrow byte limit when dataframe_sync is full
    """
    path = Path(path).expanduser()
    # Opening a missing path would create an empty Store
    if not path.exists():
        raise FileNotFoundError(f"Store file {path} does not exist")
    return build_store(None, sync=path, dataframe_sync=dataframe_sync, dataframe_max_bytes=dataframe_max_bytes)


def load_store_snapshot(path: Union[str, os.PathLike], *, metadata=False):
    """
    Read ordinary Store values from a file without subscribing to later changes.

    Parameters
    ----------
    path
        Store file previously created with file synchronization or save_store
    metadata
        Return the metadata saved with the Store instead of its ordinary values

    Returns
    -------
    Any
        A copy of the values as Python mappings, lists and scalars, DataFrames become in memory copies of their rows
    """
    path = Path(path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Snapshot file {path} does not exist")

    try:
        database = StoreDatabase(path, readonly=True)
    except apsw.NotADBError:
        _, value, saved = read_legacy_values(path)
        return saved if metadata else value
    from pret.dataframe import DataFrame

    def detach(value):
        """
        Replace DataFrame references by in memory copies of their rows so the file can be closed
        """
        if is_marker(value):
            return DataFrame(SQLiteDataFrameProvider(database, value[DATAFRAME_REF_KEY]).materialize())
        if isinstance(value, dict):
            return {key: detach(child) for key, child in value.items()}
        if isinstance(value, list):
            return [detach(child) for child in value]
        return value

    try:
        state, _ = database.read_state()
        return state["metadata"] if metadata else detach(state["data"])
    finally:
        database.close()


@marshal_as(
    js="""
return window.storeLib.snapshot;
"""
)
def snapshot(value):
    raise NotImplementedError(
        "This function is a placeholder for the JavaScript implementation. It should not be called in Python."
    )


@marshal_as(
    js="""
return (function(store, callback) {
    if (arguments.length > 0) {
        var kwargs = arguments[arguments.length - 1]
        if (kwargs && kwargs.hasOwnProperty("__kwargtrans__")) {
            delete kwargs.__kwargtrans__;
            for (var attr in kwargs) {
                switch (attr) {
                    case 'store': var store = kwargs [attr]; break;
                }
            }
        }
    }
    if (callback === undefined) {
        return (callback) => {
            return window.storeLib.subscribe(store, callback);
        }
    }
    return window.storeLib.subscribe(store, callback);
})
"""
)
def subscribe(store, callback=None):
    """
    Call a function after each saved change of a Store value.

    The callback receives the operations of the change under this value, with paths relative to it, e.g.
    `[{"op": "set", "path": [0, "done"], "value": True}]`, and the change `{"revision", "origin", "kind"}`.

    Parameters
    ----------
    store : Store
        The value to watch
    callback : callable, optional
        The function to call, subscribe returns a decorator when omitted

    Returns
    -------
    callable
        A function that stops the notifications
    """
    if callback is None:
        return lambda callback: subscribe(store, callback)
    entry = (store.node, callback)
    store.doc.subscribers.append(entry)

    def unsubscribe():
        if entry in store.doc.subscribers:
            store.doc.subscribers.remove(entry)

    return unsubscribe


@marshal_as(
    js="""
return (function(store, origin) {
    const _enter = () => {
        const txRes = window.storeLib.beginTransaction(store, origin);
        res.__exit__ = txRes[1];
        return txRes[0];
    };
    const res = {
        "__enter__": _enter,
        "__exit__": () => {},
    };
    return res;
})
"""
)
@contextmanager
def transact(store: Store, origin: Any = None):
    """
    Group multiple store mutations into a single transaction.

    Use this as a context manager to batch several writes so subscribers and
    synchronization hooks are triggered once after the block completes.

    Parameters
    ----------
    store : Store
        Store value whose document transaction should be used
    origin : Any, optional
        Origin recorded with the change, browser page origins enter the page undo history

    Yields
    -------
    dict
        The Store action being saved, with its request ID, origin and revision number
    """
    with store.doc.edit_transaction(origin=origin) as action:
        yield action
