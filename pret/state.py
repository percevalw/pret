"""
Tracked state management for Python to enable reactive programming in Python,
both in the browser and in the kernel.
Inspired by the amazing valtio library (https://github.com/pmndrs/valtio).
"""

import asyncio
import typing
import uuid
from typing import Callable, Optional, Tuple, Union
from weakref import WeakKeyDictionary, WeakValueDictionary

from .bridge import create_proxy, js, pyodide, to_js
from .manager import get_manager
from .ui.react import use_effect, use_ref, use_sync_external_store


def get_untracked(obj):
    if isinstance(obj, (TrackedDictProxy, TrackedListProxy)):
        return obj._proxy_object
    return obj


class Dict(dict):
    pass

    def __hash__(self):
        return id(self)


class List(list):
    pass

    def __hash__(self):
        return id(self)


class ProxyState:
    VERSION = 0
    CHECK_VERSION = 0

    def __init__(self, target, base_object=None):
        self.target = target
        self.base_object = base_object
        self.snap_cache = None
        self.check_version = ProxyState.CHECK_VERSION
        self.version = ProxyState.VERSION
        self.listeners = []
        self.child_proxy_states: typing.Dict[
            Union[str, int], Tuple[ProxyState, Optional[Callable]]
        ] = dict()

    def ensure_version(self, next_check_version=None):
        if next_check_version is None:
            ProxyState.CHECK_VERSION += 1
            next_check_version = ProxyState.CHECK_VERSION
        if len(self.listeners) == 0 and next_check_version != self.check_version:
            self.check_version = next_check_version
            for child_proxy_state, _ in self.child_proxy_states.values():
                child_version = child_proxy_state.ensure_version(next_check_version)
                if child_version > self.version:
                    self.version = child_version
        return self.version

    def notify_update(self, op, next_version=None):
        if next_version is None:
            ProxyState.VERSION += 1
            next_version = ProxyState.VERSION
        if self.version != next_version:
            self.version = next_version
            for listener in self.listeners:
                listener(op, next_version)

    def create_prop_listener(self, prop):
        def listener(op, next_version):
            new_op = list(op)
            new_op[1] = [prop, *new_op[1]]
            self.notify_update(new_op, next_version)

        return listener

    def add_prop_listener(self, prop, child_proxy_state):
        if prop in self.child_proxy_states:
            raise ValueError("prop listener already exists")
        if len(self.listeners) > 0:
            remove = child_proxy_state.add_listener(self.create_prop_listener(prop))
            self.child_proxy_states[prop] = (child_proxy_state, remove)
        else:
            self.child_proxy_states[prop] = (child_proxy_state, None)

    def remove_prop_listener(self, prop):
        entry = self.child_proxy_states.pop(prop, None)
        if entry is not None and entry[1] is not None:
            entry[1]()

    def add_listener(self, listener: Callable):
        self.listeners.append(listener)

        # If this is the first listener, add prop listeners to all child proxy states
        # Otherwise, this means that the child proxy states already have prop listeners
        # that will trigger us to update, so we don't need to add these again
        if len(self.listeners) == 1:
            for prop, (child_proxy_state, _) in self.child_proxy_states.items():
                remove = child_proxy_state.add_listener(self.create_prop_listener(prop))
                self.child_proxy_states[prop] = (child_proxy_state, remove)

        def remove_listener():
            if listener in self.listeners:
                self.listeners.remove(listener)
            if len(self.listeners) == 0:
                for prop, (
                    child_proxy_state,
                    remove,
                ) in self.child_proxy_states.items():
                    if remove is not None:
                        remove()
                        self.child_proxy_states[prop] = (child_proxy_state, None)

        return remove_listener

    def get_snapshot(self):
        self.ensure_version()
        if self.snap_cache is not None and self.snap_cache[0] == self.version:
            return self.snap_cache[1]

        if isinstance(self.target, list):
            snap = List()
            self.snap_cache = (self.version, snap)
            for value in self.target:
                snap.append(snapshot(value))
        elif isinstance(self.target, dict):
            snap = Dict()
            self.snap_cache = (self.version, snap)
            for key, value in self.target.items():
                snap[key] = snapshot(value)
        else:
            raise ValueError("target should be list or dict")

        return snap


class DictProxy(dict):
    def __init__(self, mapping, sync_id=None):
        super().__init__()

        self.proxy_state = ProxyState(self, mapping)
        proxy_state_map[self] = self.proxy_state

        # Assign keys before register synchronization handlers
        for key, value in mapping.items():
            self[key] = value

        if sync_id is not None:
            self._sync_id = sync_id
            manager = get_manager()

            def listener(ops):
                return manager.send_state_change(ops, sync_id)

            unsub = subscribe(self, listener)
            manager.register_state(sync_id, self, unsub)

    def _patch(self, ops):
        patch(self, ops)

    def __reduce__(self):
        return DictProxy, (
            self.proxy_state.get_snapshot(),
            getattr(self, "_sync_id", None),
        )

    def __setitem__(self, key, value):
        has_prev_value = key in self
        prev_value = self.get(key)
        if has_prev_value and value is prev_value:
            return

        # Re-assign prop listener
        proxy_state = proxy_state_map[self]
        proxy_state.remove_prop_listener(key)

        if isinstance(value, (dict, list)):
            value = get_untracked(value) or value

        # Ensure that the value is proxied
        if value not in proxy_state_map:  # and proxied not in ref_set:
            proxied = proxy(value)
            created_a_proxy = value is not proxied and is_proxy(proxied)
        else:
            proxied = value
            created_a_proxy = False

        # If a proxy was created (nested object), add a prop listener to it
        if created_a_proxy:
            child_proxy_state = proxy_state_map.get(proxied)
            if child_proxy_state:
                proxy_state.add_prop_listener(key, child_proxy_state)

        super().__setitem__(key, proxied)
        proxy_state.notify_update(["__setitem__", [key], snapshot(value)])

    def __delitem__(self, key):
        super().__delitem__(key)
        self.proxy_state.remove_prop_listener(key)
        self.proxy_state.notify_update(["__delitem__", [key], None])

    def clear(self) -> None:
        for key in self:
            self.proxy_state.remove_prop_listener(key)
        super().clear()
        self.proxy_state.notify_update(["clear", [], None])

    def pop(self, key, default=None):
        self.proxy_state.remove_prop_listener(key)
        value = super().pop(key, default)
        self.proxy_state.notify_update(["__delitem__", [key], None])
        return value

    def popitem(self):
        key, value = super().popitem()
        self.proxy_state.remove_prop_listener(key)
        self.proxy_state.notify_update(["__delitem__", [key], None])
        return key, value

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def update(self, other=None, **kwargs):
        if other is not None:
            for key, value in other.items():
                self[key] = value
        for key, value in kwargs.items():
            self[key] = value

    def __hash__(self):
        return id(self)


class ListProxy(list):
    def __init__(self, sequence, sync_id=None):
        super().__init__()

        self.proxy_state = ProxyState(self, sequence)
        proxy_state_map[self] = self.proxy_state

        # Assign keys before register synchronization handlers
        for value in sequence:
            self.append(value)

        if sync_id is not None:
            self._sync_id = sync_id
            manager = get_manager()

            def listener(ops):
                return manager.send_state_change(ops, sync_id)

            unsub = subscribe(self, listener)
            manager.register_state(sync_id, self, unsub)

    def _patch(self, ops):
        patch(self, ops)

    def __reduce__(self):
        return ListProxy, (
            self.proxy_state.get_snapshot(),
            getattr(self, "_sync_id", None),
        )

    def __setitem__(self, key, value):
        prev_value = self[key]
        if value is prev_value:
            return

        # Re-assign prop listener
        proxy_state = proxy_state_map[self]
        proxy_state.remove_prop_listener(key)

        if isinstance(value, (dict, list)):
            value = get_untracked(value) or value

        # Ensure that the value is proxied
        if value not in proxy_state_map:  # and proxied not in ref_set:
            proxied = proxy(value)
            created_a_proxy = value is not proxied and is_proxy(proxied)
        else:
            proxied = value
            created_a_proxy = False

        # If a proxy was created (nested object), add a prop listener to it
        if created_a_proxy:
            child_proxy_state = proxy_state_map.get(proxied)
            if child_proxy_state:
                proxy_state.add_prop_listener(key, child_proxy_state)

        super().__setitem__(key, proxied)
        proxy_state.notify_update(["__setitem__", [key], value])

    def __delitem__(self, key):
        super().__delitem__(key)
        self.proxy_state.remove_prop_listener(key)
        self.proxy_state.notify_update(["__delitem__", [key], None])

    def pop(self, index=-1):
        value = super().pop(index)
        self.proxy_state.notify_update(["__delitem__", [index], None])
        return value

    def append(self, value) -> None:
        # Re-assign prop listener
        proxy_state = proxy_state_map[self]

        if isinstance(value, (dict, list)):
            value = get_untracked(value) or value

        # Ensure that the value is proxied
        if value not in proxy_state_map:  # and proxied not in ref_set:
            proxied = proxy(value)
            created_a_proxy = value is not proxied and is_proxy(proxied)
        else:
            proxied = value
            created_a_proxy = False

        # If a proxy was created (nested object), add a prop listener to it
        if created_a_proxy:
            child_proxy_state = proxy_state_map.get(proxied)
            if child_proxy_state:
                proxy_state.add_prop_listener(len(self), child_proxy_state)

        super().append(proxied)
        proxy_state.notify_update(["append", [], snapshot(value)])

    def extend(self, values) -> None:
        for value in values:
            self.append(value)

    def __hash__(self):
        return id(self)


class TrackedProxyState:
    def __init__(self, target, affected, base_object=None):
        self.affected = affected
        self.target = target
        self.base_object = base_object


class Affected:
    def __init__(self, base_object=None):
        self.getitem_keys = set()
        self.hasitem_keys = set()
        self.length = False
        self.base_object = base_object

    def __repr__(self):
        return f"Affected({self.getitem_keys}, {self.hasitem_keys})"


class TrackedDictProxy(dict):
    def __init__(self, mapping, affected, proxy_object):
        super().__init__(mapping)
        self._affected = affected
        self._base_object: DictProxy = mapping
        self._proxy_object = proxy_object
        # the only reason for this is to keep tracked item into memory
        # and avoid collection by the garbage collector when converting
        # them into js objects
        # fixing pyodide cache mechanism (store them in a context ?)
        # would likely be a better solution
        self._children = set()

    def __getitem__(self, item):
        if self._base_object not in self._affected:
            self_affected = self._affected[id(self._base_object)] = Affected(
                self._base_object
            )
        else:
            self_affected = self._affected[id(self._base_object)]
        self_affected.getitem_keys.add(item)
        res = tracked(
            super().__getitem__(item),
            self._affected,
            self._proxy_object[item],
        )
        self._children.add(res)
        return res

    # TODO: add __len__ and other data getters

    def __setitem__(self, key, value):
        self._proxy_object[key] = value

    def __delitem__(self, key):
        del self._proxy_object[key]

    def update(self, other=None, **kwargs):
        self._proxy_object.update(other, **kwargs)

    def pop(self, key, default=None):
        return self._proxy_object.pop(key, default)

    def popitem(self):
        return self._proxy_object.popitem()

    def clear(self):
        self._proxy_object.clear()

    def setdefault(self, key, default=None):
        return self._proxy_object.setdefault(key, default)

    def __hash__(self):
        return id(self)


class TrackedListProxy(list):
    def __init__(self, sequence, affected, proxy_object):
        super().__init__(sequence)
        self._affected = affected
        self._base_object = sequence
        self._proxy_object = proxy_object
        # see TrackedDictProxy for explanation
        self._children = set()

    def __getitem__(self, item):
        if self._base_object not in self._affected:
            self_affected = self._affected[id(self._base_object)] = Affected(
                self._base_object
            )
        else:
            self_affected = self._affected[id(self._base_object)]
        self_affected.getitem_keys.add(item)
        res = tracked(
            super().__getitem__(item),
            self._affected,
            self._proxy_object[item],
        )
        self._children.add(res)
        return res

    def __iter__(self):
        # return super().__iter__()
        if self._base_object not in self._affected:
            self_affected = self._affected[id(self._base_object)] = Affected(
                self._base_object
            )

        else:
            self_affected = self._affected[id(self._base_object)]
        for item, (value, proxy_value) in enumerate(
            zip(super().__iter__(), self._proxy_object)
        ):
            self_affected.getitem_keys.add(item)
            res = tracked(value, self._affected, proxy_value)
            self._children.add(res)
            yield res

    def __len__(self):
        if self._base_object not in self._affected:
            self_affected = self._affected[id(self._base_object)] = Affected(
                self._base_object
            )
        else:
            self_affected = self._affected[id(self._base_object)]
        self_affected.length = True
        return super().__len__()

    def append(self, value):
        self._proxy_object.append(value)

    def extend(self, values):
        self._proxy_object.extend(values)

    def insert(self, index, value):
        self._proxy_object.insert(index, value)

    def remove(self, value):
        self._proxy_object.remove(value)

    def pop(self, index=-1):
        return self._proxy_object.pop(index)

    def clear(self):
        self._proxy_object.clear()

    def __setitem__(self, key, value):
        self._proxy_object[key] = value

    def __delitem__(self, key):
        del self._proxy_object[key]

    def __hash__(self):
        return id(self)


class TrackedTupleProxy:
    def __getattribute__(self, attr):
        if attr == "__class__":
            return tuple
        return super().__getattribute__(attr)

    def __init__(self, sequence, affected, proxy_object):
        self._affected = affected
        self._base_object = sequence
        self._proxy_object = proxy_object
        # see TrackedDictProxy for explanation
        self._children = set()

    def __getitem__(self, item):
        if self._base_object not in self._affected:
            self_affected = self._affected[id(self._base_object)] = Affected(
                self._base_object
            )
        else:
            self_affected = self._affected[id(self._base_object)]
        self_affected.getitem_keys.add(item)
        res = tracked(
            self._base_object[item],
            self._affected,
            self._proxy_object[item],
        )
        self._children.add(res)
        return res

    def __iter__(self):
        len(self)
        # return super().__iter__()
        if self._base_object not in self._affected:
            self_affected = self._affected[id(self._base_object)] = Affected(
                self._base_object
            )

        else:
            self_affected = self._affected[id(self._base_object)]
        for item, (value, proxy_value) in enumerate(
            zip(self._base_object, self._proxy_object)
        ):
            self_affected.getitem_keys.add(item)
            res = tracked(value, self._affected, proxy_value)
            self._children.add(res)
            yield res

    def __len__(self):
        if self._base_object not in self._affected:
            self_affected = self._affected[id(self._base_object)] = Affected(
                self._base_object
            )
        else:
            self_affected = self._affected[id(self._base_object)]
        self_affected.length = True
        return len(self._base_object)

    def __hash__(self):
        return id(self)


def is_changed(prev_snap, next_snap, affected):
    if prev_snap is next_snap:
        return False

    if type(prev_snap) is not type(next_snap):
        return True

    if isinstance(prev_snap, (int, float, str, bool, type(None))) or isinstance(
        next_snap, (int, float, str, bool, type(None))
    ):
        return prev_snap != next_snap

    prev_affected = affected.get(prev_snap)

    if prev_affected is None:
        return True

    if prev_affected.length and len(prev_snap) != len(next_snap):
        return True

    for key in prev_affected.hasitem_keys:
        changed = (key in prev_snap) != (key in next_snap)
        if changed:
            return changed

    for key in prev_affected.getitem_keys:
        changed = is_changed(
            prev_snap[key]
            if (
                isinstance(prev_snap, dict)
                and key in prev_snap
                or 0 <= key < len(prev_snap)
            )
            else None,
            next_snap[key]
            if (
                isinstance(next_snap, dict)
                and key in next_snap
                or 0 <= key < len(next_snap)
            )
            else None,
            affected,
        )
        if changed:
            return changed

    return False


proxy_cache = WeakValueDictionary()
tracked_proxy_cache = WeakValueDictionary()
proxy_state_map: WeakKeyDictionary = WeakKeyDictionary()


def proxy(value, remote_sync=False):
    if remote_sync:
        if remote_sync is True:
            sync_id = str(uuid.uuid4())
        else:
            sync_id = remote_sync
    else:
        sync_id = None

    if isinstance(value, (int, float, str, bool, type(None))):
        return value

    weak_handle = id(value)
    proxied = proxy_cache.get(weak_handle)
    if proxied and proxy_state_map[proxied].base_object is value:
        return proxied

    if isinstance(value, dict):
        proxied = DictProxy(Dict(value), sync_id=sync_id)
    elif isinstance(value, list):
        proxied = ListProxy(List(value), sync_id=sync_id)
    elif isinstance(value, tuple):
        return tuple(proxy(item) for item in value)
    else:
        raise NotImplementedError(f"Cannot proxy {type(value)}")

    proxy_cache[weak_handle] = proxied

    return proxied


def tracked(value, affected, proxy_object):
    if isinstance(value, (int, float, str, bool, type(None))):
        return value

    weak_handle = id(value)
    proxied = tracked_proxy_cache.get(weak_handle)
    if proxied and proxied._base_object is value:
        return proxied

    if isinstance(value, dict):
        proxied = TrackedDictProxy(value, affected, proxy_object)
    elif isinstance(value, list):
        proxied = TrackedListProxy(value, affected, proxy_object)
    elif isinstance(value, tuple):
        proxied = TrackedTupleProxy(value, affected, proxy_object)
    else:
        raise NotImplementedError(f"Cannot track {type(value)}")

    tracked_proxy_cache[weak_handle] = proxied

    return proxied


def make_subscriber(subscribe, proxy_object, callback, notify_in_sync=False):
    def _subscribe():
        return subscribe(proxy_object, callback, notify_in_sync)

    return _subscribe


def subscribe(proxy_object, callback, notify_in_sync=False):
    proxy_state = proxy_state_map.get(proxy_object)
    if not proxy_state:
        raise ValueError("Please use proxy object")

    ops = []
    future = None
    is_listener_active = False

    def listener(op, next_version=None):
        nonlocal future
        ops.append(op)
        if notify_in_sync:
            callback(list(ops))
            ops.clear()
            return

        if not future:

            async def callback_and_clear_future():
                nonlocal future
                future = None
                if is_listener_active:
                    callback(list(ops))
                    ops.clear()

            future = asyncio.get_event_loop().create_task(callback_and_clear_future())

    def unsubscribe():
        nonlocal is_listener_active
        is_listener_active = False
        remove_listener()

        return make_subscriber(subscribe, proxy_object, callback, notify_in_sync)

    remove_listener = proxy_state.add_listener(listener)
    is_listener_active = True

    return unsubscribe


def snapshot(value):
    if is_proxy(value):
        proxy_state = proxy_state_map[value]
        return proxy_state.get_snapshot()
    return value


def patch(proxy_object, ops):
    # super_cls = type(proxy_object).__bases__[0]
    for op in ops:
        # apply changes to the state following the op structures used in notify_update
        # proxy_state.notify_update(["__setitem__", [path keys ...], value])
        # self.proxy_state.notify_update(["__delitem__", [path keys ...], None])
        # self.proxy_state.notify_update(["clear", [], None])

        path = op[1]
        target = proxy_object
        value = op[2]

        if op[0] in ("__setitem__", "__delitem__", "insert"):
            for key in path[:-1]:
                target = target[key]
            key = path[-1]

            if op[0] == "__setitem__":
                target[key] = value

            elif op[0] == "__delitem__":
                del target[key]

            elif op[0] == "insert":
                target.insert(key, value)
        else:
            for key in path:
                target = target[key]

            if op[0] == "clear":
                target.clear()

            elif op[0] == "append":
                target.append(value)

            elif op[0] == "extend":
                target.extend(value)


def is_proxy(obj):
    return obj.__class__.__name__.endswith("Proxy")


def use_tracked(proxy_object, notify_in_sync=False):
    if not is_proxy(proxy_object):
        raise ValueError("use_tracked can only be used with proxy objects")
    last_snapshot = use_ref(None)
    last_affected = use_ref(None)
    in_render = True

    def external_store_subscribe(callback):
        unsub = subscribe(proxy_object, callback, notify_in_sync)
        return unsub

    def external_store_get_snapshot():
        try:
            next_snapshot = snapshot(proxy_object)
        except KeyError:
            return pyodide.js.undefined
        if (
            not in_render
            and last_snapshot.current
            and last_affected.current
            and not is_changed(
                last_snapshot.current["wrapped"], next_snapshot, last_affected.current
            )
        ):
            return last_snapshot.current

        res = to_js(next_snapshot, wrap=True)
        return res

    # we don't use lambda's because of serialization issues
    def make_proxied_external_store_subscribe():
        return create_proxy(external_store_subscribe)

    def make_proxied_external_store_get_snapshot():
        return create_proxy(external_store_get_snapshot)

    curr_snapshot = use_sync_external_store(
        js.React.useMemo(
            make_proxied_external_store_subscribe,
            pyodide.ffi.to_js([]),
        ),
        js.React.useMemo(
            make_proxied_external_store_get_snapshot,
            pyodide.ffi.to_js([]),
        ),
    )

    in_render = False
    curr_affected = dict()

    def side_effect():
        last_snapshot.current = curr_snapshot
        last_affected.current = curr_affected

    # No dependencies, will run once after each render -> create_once_callable
    use_effect(pyodide.ffi.create_once_callable(side_effect))
    return tracked(curr_snapshot["wrapped"], curr_affected, proxy_object)
