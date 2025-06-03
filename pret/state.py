"""
Tracked state management for Python to enable reactive programming in Python,
both in the browser and in the kernel.
Inspired by the amazing valtio library (https://github.com/pmndrs/valtio).
Since v0.2.0, Pret proxies are actually marshaled as valtio proxies when sending
the app to the browser.
"""

import asyncio
import typing
import uuid
from typing import Callable, Optional, Tuple, Union, overload
from weakref import WeakKeyDictionary, WeakValueDictionary

import pret
from pret.marshal import marshal_as


@marshal_as(dict)
class Dict(dict):
    pass

    def __hash__(self):
        return id(self)


@marshal_as(list)
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

        # If this is the first listener, add prop listeners to all child proxy states.
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


@marshal_as(js="return window.valtio.proxy")
class DictPretProxy(dict):
    def __init__(self, mapping, sync_id=None):
        super().__init__()

        self.proxy_state = ProxyState(self, mapping)
        proxy_state_map[self] = self.proxy_state

        # Assign keys before register synchronization handlers
        for key, value in mapping.items():
            self[key] = value

        if sync_id is not None:
            self._sync_id = sync_id
            manager = pret.manager.get_manager()

            def listener(ops):
                return manager.send_state_change(ops, sync_id)

            unsub = subscribe(self, listener)
            manager.register_state(sync_id, self, unsub)

    def _patch(self, ops):
        patch(self, ops)

    def __reduce__(self):
        return make_reconstruct_js_proxy(), (
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
        proxy_state.notify_update(["set", [key], snapshot(value)])

    def __delitem__(self, key):
        super().__delitem__(key)
        self.proxy_state.remove_prop_listener(key)
        self.proxy_state.notify_update(["delete", [key], None])

    def clear(self) -> None:
        for key in self:
            self.proxy_state.remove_prop_listener(key)
        super().clear()
        self.proxy_state.notify_update(["clear", [], None])

    def pop(self, key, default=None):
        self.proxy_state.remove_prop_listener(key)
        value = super().pop(key, default)
        self.proxy_state.notify_update(["delete", [key], None])
        return value

    def popitem(self):
        key, value = super().popitem()
        self.proxy_state.remove_prop_listener(key)
        self.proxy_state.notify_update(["delete", [key], None])
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


@marshal_as(js="return window.valtio.proxy")
class ListPretProxy(list):
    def __init__(self, sequence, sync_id=None):
        super().__init__()

        self.proxy_state = ProxyState(self, sequence)
        proxy_state_map[self] = self.proxy_state

        # Assign keys before register synchronization handlers
        for value in sequence:
            self.append(value)

        if sync_id is not None:
            self._sync_id = sync_id
            manager = pret.manager.get_manager()

            def listener(ops):
                return manager.send_state_change(ops, sync_id)

            unsub = subscribe(self, listener)
            manager.register_state(sync_id, self, unsub)

    def _patch(self, ops):
        patch(self, ops)

    def __reduce__(self):
        return make_reconstruct_js_proxy(), (
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
        proxy_state.notify_update(["set", [key], value])

    def __delitem__(self, key):
        super().__delitem__(key)
        self.proxy_state.remove_prop_listener(key)
        self.proxy_state.notify_update(["delete", [key], None])

    def pop(self, index=-1):
        value = super().pop(index)
        self.proxy_state.notify_update(["delete", [index], None])
        return value

    def append(self, value) -> None:
        # Re-assign prop listener
        proxy_state = proxy_state_map[self]

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


def is_changed(prev_snap, next_snap, affected):
    if prev_snap is next_snap:
        return False

    if type(prev_snap) is not type(next_snap):
        return True

    if isinstance(prev_snap, (int, float, str, bool, type(None))) or isinstance(
        next_snap, (int, float, str, bool, type(None))
    ):
        return prev_snap != next_snap

    prev_affected = affected.get(id(prev_snap))

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
                key in prev_snap
                if isinstance(prev_snap, dict)
                else 0 <= key < len(prev_snap)
            )
            else None,
            next_snap[key]
            if (
                key in next_snap
                if isinstance(next_snap, dict)
                else 0 <= key < len(next_snap)
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


T = typing.TypeVar("T")


@overload
def proxy(value: typing.Mapping, remote_sync: bool = False) -> DictPretProxy: ...


@overload
def proxy(value: typing.Sequence, remote_sync: bool = False) -> ListPretProxy: ...


@overload
def proxy(value: T, remote_sync: bool = False) -> T: ...


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
        proxied = DictPretProxy(Dict(value), sync_id=sync_id)
    elif isinstance(value, list):
        proxied = ListPretProxy(List(value), sync_id=sync_id)
    elif isinstance(value, tuple):
        return tuple(proxy(item) for item in value)
    else:
        raise NotImplementedError(f"Cannot proxy {type(value)}")

    proxy_cache[weak_handle] = proxied

    return proxied


TrackedDictPretProxy = dict
TrackedListPretProxy = list


def make_subscriber(subscribe, proxy_object, callback, notify_in_sync=False):
    def _subscribe():
        return subscribe(proxy_object, callback, notify_in_sync)

    return _subscribe


@marshal_as(
    js="""
return (function(proxy_object, callback, notify_in_sync) {
    if (arguments.length > 0) {
        var kwargs = arguments[arguments.length - 1]
        if (kwargs && kwargs.hasOwnProperty("__kwargtrans__")) {
            delete props.__kwargtrans__;
            for (var attr in kwargs) {
                switch (attr) {
                    case 'proxy_object': var proxy_object = kwargs [attr]; break;
                    case 'notify_in_sync': var notify_in_sync = kwargs [attr]; break;
                }
            }
        }
    }
    if (callback === undefined) {
        return (callback) => {
            return window.valtio.subscribe(proxy_object, callback, notify_in_sync);
        }
    }
    return window.valtio.subscribe(proxy_object, callback, notify_in_sync);
})
"""
)
def subscribe(proxy_object, callback=None, notify_in_sync=False):
    if callback is None:

        def decorator(func):
            return subscribe(proxy_object, func, notify_in_sync)

        return decorator
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


@marshal_as(
    js="""
return ((obj) => {
    window.valtio.trackMemo(obj);
    return window.valtio.getUntracked(obj) || window.valtio.snapshot(obj);
})
"""
)
def snapshot(value):
    if is_proxy(value):
        proxy_state = proxy_state_map[value]
        return proxy_state.get_snapshot()
    return value


def patch(proxy_object, ops):
    for op in ops:
        # apply changes to the state following the op structures used in notify_update
        # proxy_state.notify_update(["set", [path keys ...], value])
        # self.proxy_state.notify_update(["delete", [path keys ...], None])
        # self.proxy_state.notify_update(["clear", [], None])

        path = op[1]
        target = proxy_object
        value = op[2]

        if op[0] in ("set", "delete", "insert"):
            for key in path[:-1]:
                if isinstance(target, (list, tuple)):
                    key = int(key)
                target = target[key]

            key = path[len(path) - 1]
            if isinstance(target, (list, tuple)):
                key = int(key)

            if op[0] == "set":
                if isinstance(target, (list, tuple)):
                    if key == "length":
                        if len(target) < value:
                            target.extend([None] * (value - len(target)))
                        elif len(target) > value:
                            del target[value:]
                    else:
                        if key >= len(target) and isinstance(target, list):
                            target.extend([None] * (key - len(target) + 1))
                        target[key] = value
                else:
                    target[key] = value

            elif op[0] == "delete":
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
    return obj.__class__.__name__.endswith("PretProxy")


_reconstruct_js_proxy = None


def make_reconstruct_js_proxy():
    global _reconstruct_js_proxy
    if _reconstruct_js_proxy is not None:
        return _reconstruct_js_proxy

    @marshal_as(
        js="""
return (function reconstruct_js_proxy(content, sync_id) {
    var proxy = window.valtio.proxy(content);
    if (sync_id) {
        var manager = get_manager();
        unsub = window.valtio.subscribe(proxy, function(ops) {
            manager.send_state_change(ops, sync_id);
        }, false);
        manager.register_state(sync_id, proxy, unsub);
    }
    return proxy;
});""",
        globals=lambda: {"get_manager": pret.manager.get_manager},
    )
    def reconstruct_js_proxy(content, sync_id=None): ...

    _reconstruct_js_proxy = reconstruct_js_proxy

    return _reconstruct_js_proxy
