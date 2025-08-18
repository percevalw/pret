from __future__ import annotations

import asyncio
import os
import threading
import uuid
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional, Union

from pycrdt._array import Array
from pycrdt._base import BaseDoc, BaseType, _find_path, _rebuild_obj, base_types
from pycrdt._doc import Doc
from pycrdt._map import Map
from pycrdt._pycrdt import Array as _Array
from pycrdt._pycrdt import Doc as _Doc
from pycrdt._pycrdt import Map as _Map
from watchfiles import awatch

from pret.manager import get_manager
from pret.marshal import marshal_as


def _convert(value: Any) -> Any:
    if isinstance(value, (BaseType, BaseDoc)):
        return value
    if isinstance(value, list):
        return AutoArray(value)
    if isinstance(value, dict):
        return AutoMap(value)
    # if isinstance(value, str):
    #     return Text(value)
    return value


class AutoArray(Array):
    """Array subclass to automatically convert assigned values to container types."""

    def __init__(self, init=None, *, _doc=None, _integrated=None):
        super().__init__(init=init, _doc=_doc, _integrated=_integrated)
        self._type_name = "array"

    def _set(self, index: int, value: Any) -> None:  # type: ignore[override]
        value = _convert(value)
        super()._set(index, value)

    def __reduce__(self):
        if self._doc is None:
            return type(self), (self.to_py(),)
        path = _find_path(self.doc, self)
        return _rebuild_obj, (self.doc, tuple(path))


class AutoMap(Map):
    """Map subclass to automatically convert assigned values to container types."""

    def __init__(self, init=None, *, _doc=None, _integrated=None):
        super().__init__(init=init, _doc=_doc, _integrated=_integrated)
        self._type_name = "map"

    def _set(self, key: str, value: Any) -> None:  # type: ignore[override]
        value = _convert(value)
        super()._set(key, value)

    def __reduce__(self):
        if self._doc is None:
            return type(self), (self.to_py(),)
        path = _find_path(self.doc, self)
        return _rebuild_obj, (self.doc, tuple(path))


class AutoDoc(Doc):
    """Doc with root container values auto converted to AutoArray and AutoMap."""

    def __init__(self, *args, sync_id, **kwargs):
        super().__init__(*args, **kwargs)
        self._type_name = "doc"
        self.sync_id = sync_id

    def __setitem__(self, key: str, value: Any) -> None:  # type: ignore[override]
        value = _convert(value)
        super().__setitem__(key, value)

    def __reduce__(self):
        update = self.get_update()
        return _make_rebuild_doc(), (update, {}, self.sync_id)

    def on_update(self, fn):
        """Register a callback to be called on document updates."""
        return self.observe(lambda event: fn(event.update))

    def to_py(self) -> dict[str, Any]:
        """Convert the document to a Python dictionary."""

        def rec(value: Any) -> Any:
            if isinstance(value, (list, AutoArray)):
                return [(rec(v) if isinstance(v, BaseType) else v) for v in value]
            elif isinstance(value, (dict, AutoMap)):
                return {
                    k: (rec(v) if isinstance(v, BaseType) else v)
                    for k, v in value.items()
                }
            elif isinstance(value, BaseDoc):
                return value.to_py()
            else:
                return value

        return {k: rec(v) for k, v in self._roots.items()}


base_types[_Array] = AutoArray  # type: ignore[assignment]
base_types[_Map] = AutoMap  # type: ignore[assignment]
base_types[_Doc] = AutoDoc  # type: ignore[assignment]

marshal_as(
    _rebuild_obj,
    js="""
       return (function rebuild_obj(obj, path) {
           for (var part of path) {
               obj = obj.get(part);
           }
           var proxy = window.storeLib.makeStore(obj);
           return proxy;
       });""",
)


def _make_rebuild_doc():
    @marshal_as(
        js="""
return (function rebuild_doc(update, roots, sync_id) {
    var ydoc = new window.Y.Doc();
    ydoc.getMap("_");  // Ensure the root map exists
    ydoc.apply_update(update);
    if (sync_id) {
        var manager = get_manager();
        // Will subscribe to updates on ydoc and let the manager dispatch them
        // and apply updates to the ydoc when the manager receives them
        manager.register_state(sync_id, ydoc);
    }
    return ydoc;
});""",
        globals={
            "get_manager": get_manager,
        },
    )
    def _rebuild_doc(update: bytes, roots: dict[str, str], sync_id) -> "Doc":
        doc = AutoDoc(sync_id=sync_id)
        if update:
            doc.apply_update(update)
        return doc

    return _rebuild_doc


def create_store(
    data,
    *,
    sync: Optional[Union[bool, str, os.PathLike]] = None,
    sync_id=None,
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
    if sync and sync_id is None:
        sync_id = str(uuid.uuid4())
    doc = AutoDoc({"_": {}}, sync_id=sync_id)

    if not isinstance(sync, (str, os.PathLike)):
        doc["_"]["_"] = data
    else:
        offset = 36  # uuid prefix length
        path = Path(sync).expanduser()

        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            doc["_"]["_"] = data
            sync_bytes = doc.sync_id.encode()
            size_bytes = len(doc.get_update()).to_bytes(4, "little")
            with path.open("wb") as f:
                f.write(sync_bytes)
                f.write(size_bytes)
                f.write(doc.get_update())
                offset = f.tell()
        else:
            with path.open("rb") as f:
                existing_id = f.read(36).decode()
                sync_id = existing_id
                doc.sync_id = existing_id
                offset = f.tell()

        lock = threading.Lock()
        applying = False

        def read_updates():
            nonlocal offset, applying
            if not path.exists():
                return
            with lock, path.open("rb") as f:
                f.seek(offset)
                while True:
                    start = f.tell()
                    header = f.read(4)
                    if len(header) < 4:
                        f.seek(start)
                        break
                    size = int.from_bytes(header, "little")
                    data = f.read(size)
                    if len(data) < size:
                        f.seek(start)
                        break
                    offset = f.tell()
                    applying = True
                    doc.apply_update(data)
                    applying = False

        read_updates()

        def on_doc_update(update):
            nonlocal offset
            if applying:
                return
            size_bytes = len(update).to_bytes(4, "little")
            with lock, path.open("ab") as f:
                f.write(size_bytes)
                f.write(update)
            offset += len(size_bytes) + len(update)

        doc.on_update(on_doc_update)

        # Start an asyncio task that watches the directory for changes
        async def _watcher():
            async for changes in awatch(path):
                for change, change_path in changes:
                    if change.name != "deleted":
                        read_updates()

        loop = asyncio.get_event_loop()

        def _start_watcher() -> None:
            watcher_task = asyncio.create_task(_watcher())
            doc._persistence_watcher = watcher_task

            def _end_watcher():
                watcher_task.cancel()

            doc._persistence_finalizer = weakref.finalize(doc, _end_watcher)

        if loop.is_running():
            _start_watcher()
        else:
            loop.call_soon(_start_watcher)

    if sync_id is not None:
        from pret.manager import get_manager

        manager = get_manager()
        manager.register_state(sync_id, doc)

    return doc["_"]["_"]


@marshal_as(
    js="""
return window.storeLib.snapshot;
"""
)
def snapshot(value):
    raise NotImplementedError(
        "This function is a placeholder for the JavaScript implementation. "
        "It should not be called in Python."
    )


@marshal_as(
    js="""
return (function(store, callback, notify_in_sync) {
    if (arguments.length > 0) {
        var kwargs = arguments[arguments.length - 1]
        if (kwargs && kwargs.hasOwnProperty("__kwargtrans__")) {
            delete kwargs.__kwargtrans__;
            for (var attr in kwargs) {
                switch (attr) {
                    case 'store': var store = kwargs [attr]; break;
                    case 'notify_in_sync': var notify_in_sync = kwargs [attr]; break;
                }
            }
        }
    }
    if (callback === undefined) {
        return (callback) => {
            return window.storeLib.subscribe(store, callback, notify_in_sync);
        }
    }
    return window.storeLib.subscribe(store, callback, notify_in_sync);
})
"""
)
def subscribe(store, callback=None, notify_in_sync=False):
    """
    Subscribe to changes in a store.

    Parameters
    ----------
    store : Any
        The store to subscribe to.
    callback : callable, optional
        The function to call when the object changes.
    notify_in_sync : bool, optional
        If True, the callback will be called in sync with the change.
        Only relevant in the browser environment.

    Returns
    -------
    callable
        A function that can be used to unsubscribe from the changes.
    """
    store.observe_deep(callback)


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
def transact(store: Union[AutoArray, AutoMap], origin: Any = None):
    with store.doc.transaction(origin):
        yield store
