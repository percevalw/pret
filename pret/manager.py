"""
This module provides client and server managers for handling remote calls,
state synchronization, and communication between the frontend and backend.
"""

import asyncio
import base64
import gzip
import hashlib
import inspect
import uuid
from asyncio import Future
from json import dumps, loads
from typing import Any, Awaitable, Callable, Union
from weakref import WeakKeyDictionary, WeakValueDictionary, ref

from typing_extensions import ParamSpec, TypeVar

from pret.marshal import marshal_as

CallableParams = ParamSpec("ServerCallableParams")
CallableReturn = TypeVar("CallableReturn")
UNSET = object()
PRET_BUNDLE_GZIP_MIN_BYTES = 256 * 1024
STATE_CHANGE_TIMEOUT = 5000
STATE_SYNC_TIMEOUT = 5000


def make_remote_callable(function_id):
    async def remote_call(*args, **kwargs):
        return await get_manager().send_call(function_id, args, kwargs)

    return remote_call


def server_only(
    fn: Callable[CallableParams, CallableReturn],
) -> Callable[CallableParams, Union[Awaitable[CallableReturn], CallableReturn]]:
    return marshal_as(fn, make_remote_callable(get_manager().register_function(fn)))


@marshal_as(
    js="""
return function is_awaitable(value) {
   return true;
}
""",
    globals={},
)
def is_awaitable(value):
    """If the value is an awaitable, await it, otherwise return the value."""
    return inspect.isawaitable(value)


@marshal_as(
    js="""
return function start_async_task(task) {
    return task;
}
""",
    globals={},
)
def start_async_task(task):
    """Start an async task and return it."""

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()
        return loop.create_task(task)
    return loop.create_task(task)


@marshal_as(
    js="""
return function schedule_timeout(callback, timeout) {
    return setTimeout(callback, timeout);
}
""",
    globals={},
)
def schedule_timeout(callback, timeout):
    """Schedule a timeout in the current runtime."""

    try:
        loop = asyncio.get_running_loop()
    except Exception:
        try:
            loop = asyncio.get_event_loop()
        except Exception:
            return None
    return loop.call_later(timeout / 1000, callback)


@marshal_as(
    js="""
return (resource, options) => {
    return fetch(resource, options);
}
""",
    globals={},
)
async def fetch(resource, options): ...


@marshal_as(
    js="""
return function function_identifier(func) {
    throw new Error("function_identifier is not implemented in JavaScript");
}
""",
    globals={},
)
def function_identifier(func):
    import inspect

    module = inspect.getmodule(func)
    module_name = module.__name__ if module is not None else "__main__"
    qual_name = func.__qualname__

    identifier = f"{module_name}.{qual_name}"

    if inspect.isfunction(func) and func.__closure__:
        code = func.__code__
        code_str = str(code.co_code) + str(code.co_consts) + str(code.co_varnames)
        if func.__defaults__:
            defaults_str = "".join(str(x) for x in func.__defaults__)
            code_str += defaults_str

        code_hash = hashlib.md5(code_str.encode("utf-8")).hexdigest()
        identifier = f"{identifier}.{code_hash}"

    return identifier


class weakmethod:
    def __init__(self, cls_method):
        self.cls_method = cls_method
        self.instance = None
        self.owner = None

    def __get__(self, instance, owner):
        self.instance = ref(instance)
        self.owner = owner
        return self

    def __call__(self, *args, **kwargs):
        if self.owner is None:
            raise Exception(
                "Function was never bound to a class scope, you should use it as a "
                "decorator on a method"
            )
        instance = self.instance()
        if instance is None:
            raise Exception(
                f"Cannot call {self.owner.__name__}.{self.cls_method.__name__} because "
                f"instance has been destroyed"
            )
        return self.cls_method(instance, *args, **kwargs)


@marshal_as(
    js="""
return () => {
   const cryptoObj = (globalThis.crypto || globalThis.msCrypto);
   if (!cryptoObj?.getRandomValues) {
       throw new Error("Secure RNG unavailable: crypto.getRandomValues not supported.");
   }

   const bytes = new Uint8Array(16);
   cryptoObj.getRandomValues(bytes);

   // RFC 4122 version & variant bits
   bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
   bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 10

   let hex = "";
   for (let i = 0; i < 16; i++) hex += bytes[i].toString(16).padStart(2, "0");
   return hex;
}
"""
)
def make_uuid():
    return uuid.uuid4().hex


@marshal_as(
    js="""
return function ensure_store_undo_manager(state) {
    if (window.storeLib && typeof window.storeLib.ensureUndoManagerForDoc === "function") {
        return window.storeLib.ensureUndoManagerForDoc(state);
    }
    return null;
}
""",
    globals={},
)
def ensure_store_undo_manager(state):
    return None


@marshal_as(
    js="""
return function install_store_sync_guard(state, guard) {
    if (window.storeLib && typeof window.storeLib.installSyncGuardForDoc === "function") {
        window.storeLib.installSyncGuardForDoc(state, guard);
    }
}
""",
    globals={},
)
def install_store_sync_guard(state, guard):
    return None


@marshal_as(
    js="""
return function rollback_store_state(state, count) {
    if (window.storeLib && typeof window.storeLib.undoDocChanges === "function") {
        return window.storeLib.undoDocChanges(state, count);
    }
    if (window.storeLib && typeof window.storeLib.undoDoc === "function") {
        var rolledBack = 0;
        var rollbackCount = count || 1;
        for (var i = 0; i < rollbackCount; i++) {
            if (!window.storeLib.undoDoc(state)) break;
            rolledBack += 1;
        }
        return rolledBack;
    }
    return 0;
}
""",
    globals={},
)
def rollback_store_state(state, count=1):
    return 0


class ClientManager:
    manager = None

    def __init__(self):
        self.functions = {}
        self.refs = {}
        self.states: "WeakValueDictionary[str, Any]" = WeakValueDictionary()
        self.states_subscriptions: "WeakKeyDictionary[Any, Any]" = WeakKeyDictionary()
        self.call_futures = {}
        self.disabled_state_sync = set()
        self.outgoing_messages = []
        self.is_draining_outgoing_messages = False
        self.connection_state = {
            "kind": "unknown",
            "transport": None,
            "connected": None,
            "reason": None,
            "kernel_connection_status": None,
            "last_error": None,
            "state_write_rejection_count": 0,
            "last_state_write_rejection": None,
        }
        self.connection_state_listeners = set()
        self.state_sync = {}
        self.state_sync_requests = {}
        self.uid = make_uuid()
        self.current_origin = self.uid
        self.register_function(self.call_ref_method, "<ref_method>")
        self.last_messages = []

    def set_connection_status(
        self,
        connected=UNSET,
        reason=UNSET,
        transport=UNSET,
        kernel_connection_status=UNSET,
        last_error=UNSET,
    ):
        current_state = self.connection_state
        new_state = dict(current_state)
        has_changed = False

        if connected is not UNSET and connected != current_state["connected"]:
            new_state["connected"] = connected
            has_changed = True
        if reason is not UNSET and reason != current_state["reason"]:
            new_state["reason"] = reason
            has_changed = True
        if transport is not UNSET and transport != current_state["transport"]:
            new_state["transport"] = transport
            has_changed = True
        if (
            kernel_connection_status is not UNSET
            and kernel_connection_status != current_state["kernel_connection_status"]
        ):
            new_state["kernel_connection_status"] = kernel_connection_status
            has_changed = True
        if last_error is not UNSET and last_error != current_state["last_error"]:
            new_state["last_error"] = last_error
            has_changed = True

        if has_changed:
            self.connection_state = new_state
            self.notify_connection_status_listeners()
            if new_state["connected"] is True:
                self.resume_state_sync_processors()
        return self.connection_state

    def get_connection_status(self):
        return self.connection_state

    def notify_connection_status_listeners(self):
        for callback in tuple(self.connection_state_listeners):
            try:
                callback()
            except Exception:
                pass

    def subscribe_connection_status(self, callback):
        self.connection_state_listeners.add(callback)

        def unsubscribe():
            self.connection_state_listeners.discard(callback)

        return unsubscribe

    def notify_state_write_rejected(self, sync_id, reason):
        new_state = dict(self.connection_state)
        new_state["state_write_rejection_count"] = (
            self.connection_state.get("state_write_rejection_count") or 0
        ) + 1
        new_state["last_state_write_rejection"] = {
            "sync_id": sync_id,
            "reason": reason,
        }
        self.connection_state = new_state
        self.notify_connection_status_listeners()

    def assert_state_write_allowed(self, sync_id):
        connection = self.connection_state
        if connection.get("connected") is False:
            reason = connection.get("reason") or "disconnected"
            self.notify_state_write_rejected(sync_id, reason)
            raise Exception(f"Cannot write synchronized state {sync_id}: connection is {reason}")
        state_sync = self.ensure_state_sync(sync_id)
        if state_sync["status"] == "blocked":
            reason = state_sync.get("blocked_reason") or "blocked"
            raise Exception(f"Cannot write synchronized state {sync_id}: sync is {reason}")
        if state_sync["status"] == "resyncing":
            raise Exception(f"Cannot write synchronized state {sync_id}: sync is resyncing")
        return True

    def send_message(self, method, data):
        raise NotImplementedError()

    def send_outgoing_message(self, method, data, on_failure=None):
        future = Future()
        self.outgoing_messages.append(
            {
                "method": method,
                "data": data,
                "future": future,
                "on_failure": on_failure,
            }
        )
        if not self.is_draining_outgoing_messages:
            self.is_draining_outgoing_messages = True
            task = self.drain_outgoing_messages()
            if is_awaitable(task):
                start_async_task(task)
        return future

    async def drain_outgoing_messages(self):
        try:
            while len(self.outgoing_messages):
                queued = self.outgoing_messages.pop(0)
                method = queued["method"]
                data = queued["data"]
                future = queued["future"]
                on_failure = queued.get("on_failure")
                try:
                    sent = self.send_message(method, data)
                    if is_awaitable(sent):
                        await sent
                    future.set_result(None)
                except Exception as error:
                    message_text = str(error)
                    reason = (
                        "send_timeout" if "PRET_COMM_TIMEOUT" in message_text else "send_failed"
                    )
                    self.set_connection_status(
                        connected=False,
                        reason=reason,
                        last_error=message_text,
                    )
                    try:
                        if on_failure is not None:
                            on_failure(error, method, data)
                    finally:
                        if isinstance(error, Exception):
                            future.set_exception(error)
                        else:
                            future.set_exception(Exception(str(error)))
        finally:
            self.is_draining_outgoing_messages = False
            if len(self.outgoing_messages):
                self.is_draining_outgoing_messages = True
                task = self.drain_outgoing_messages()
                if is_awaitable(task):
                    start_async_task(task)

    def handle_message(self, method, data):
        self.last_messages.append([method, data])
        if method == "call":
            return self.handle_call_msg(data)
        elif method == "state_change":
            return self.handle_state_change_msg(data)
        elif method == "state_change_result":
            return self.handle_state_change_result_msg(data)
        elif method == "call_success":
            return self.handle_call_success_msg(data)
        elif method == "call_failure":
            return self.handle_call_failure_msg(data)
        elif method == "state_sync_response":
            return self.handle_state_sync_response_msg(data)
        else:
            raise Exception(f"Unknown method: {method}")

    def message_targets_this_manager(self, data):
        target_origin = data.get("target_origin") if data else None
        return not target_origin or target_origin == self.uid

    def register_ref(self, ref_id, ref):
        self.refs[ref_id] = ref

    def call_ref_method(self, ref_id, method_name, args, kwargs):
        ref = self.refs.get(ref_id)
        if not ref:
            print(f"Reference with id {ref_id} not found")
        if not ref.current:
            return None
        method = ref.current[method_name]
        return method(*args, **kwargs)

    def remote_call_ref_method(self, ref_id, method_name, args, kwargs):
        return self.send_call(
            "<ref_method>",
            (ref_id, method_name, args, kwargs),
            {},
        )

    def build_state_sync_request(self, sync_id=None):
        state_vectors = {}
        for sid, state in self.states.items():
            if not sync_id or sid == sync_id:
                state_vectors[sid] = b64_encode(state.get_state())
        payload = {
            "request_id": make_uuid(),
            "state_vectors": state_vectors,
            "origin": self.uid,
        }
        if sync_id:
            payload["sync_id"] = sync_id
        return payload

    def request_state_sync(self, sync_id=None):
        payload = self.build_state_sync_request(sync_id)
        request_id = payload["request_id"]
        sync_ids = []
        if not sync_id:
            for sid in tuple(self.states.keys()):
                state_sync = self.set_state_sync_status(sid, "resyncing")
                state_sync["resync_request_id"] = request_id
                sync_ids.append(sid)
        else:
            state_sync = self.set_state_sync_status(sync_id, "resyncing")
            state_sync["resync_request_id"] = request_id
            sync_ids.append(sync_id)

        self.state_sync_requests[request_id] = {
            "sync_ids": sync_ids,
            "active": True,
        }

        def on_timeout():
            request = self.state_sync_requests.get(request_id)
            if not request or not request.get("active"):
                return
            self.state_sync_requests.pop(request_id, None)
            for sid in tuple(request["sync_ids"]):
                state_sync = self.ensure_state_sync(sid)
                if (
                    state_sync.get("resync_request_id") == request_id
                    and state_sync["status"] == "resyncing"
                ):
                    self.block_state_sync(
                        sid,
                        "state_sync_timeout",
                        Exception("State sync timed out before the backend responded"),
                        request_resync=True,
                    )

        self.set_timeout(on_timeout, STATE_SYNC_TIMEOUT)
        return self.send_outgoing_message(
            "state_sync_request",
            payload,
        )

    def handle_state_sync_response_msg(self, data=None):
        if not self.message_targets_this_manager(data):
            return None

        sync_id = data.get("sync_id") if data else None
        request_id = data.get("request_id") if data else None
        updates = data.get("updates", {}) if data else {}
        state_vectors = data.get("state_vectors", {}) if data else {}
        origin = data.get("origin") if data else None

        if request_id and request_id in self.state_sync_requests:
            self.state_sync_requests[request_id]["active"] = False
            self.state_sync_requests.pop(request_id, None)

        for sid, update_b64 in updates.items():
            if sync_id and sid != sync_id:
                continue
            state = self.states.get(sid)
            if not state:
                continue
            state_sync = self.ensure_state_sync(sid)
            active_request_id = state_sync.get("resync_request_id")
            if active_request_id and request_id and active_request_id != request_id:
                continue
            self.apply_state_update(sid, state, b64_decode(update_b64), origin)

        for sid, state_vector in state_vectors.items():
            if sync_id and sid != sync_id:
                continue
            state = self.states.get(sid)
            if not state:
                continue
            state_sync = self.ensure_state_sync(sid)
            active_request_id = state_sync.get("resync_request_id")
            if active_request_id and request_id and active_request_id != request_id:
                continue
            update = state.get_update(b64_decode(state_vector))
            has_pending = len(state_sync["pending_changes"]) > 0 or state_sync["in_flight"]
            if len(update) and not has_pending:
                self.send_state_change(update, sid)
            state_sync["resync_request_id"] = None
            self.set_state_sync_status(sid, "ready")
            self.ensure_state_change_processor(sid)

    def send_call(self, function_id, args, kwargs):
        callback_id = make_uuid()
        payload = {
            "function_id": function_id,
            "args": args,
            "kwargs": kwargs,
            "callback_id": callback_id,
            "origin": self.uid,
        }
        future = Future()
        self.register_call_future(callback_id, future)

        def on_send_failure(error, method, data):
            call_future = self.call_futures.pop(callback_id, None)
            if call_future and not call_future.done():
                if isinstance(error, Exception):
                    call_future.set_exception(error)
                else:
                    call_future.set_exception(Exception(str(error)))

        self.send_outgoing_message("call", payload, on_failure=on_send_failure)
        return future

    def register_call_future(self, callback_id, future):
        self.call_futures[callback_id] = future

    async def handle_call_msg(self, data):
        function_id, args, kwargs, callback_id = (
            data["function_id"],
            data["args"],
            data["kwargs"],
            data["callback_id"],
        )
        try:
            fn = self.functions.get(function_id)
            # check coroutine or sync function
            result = fn(*args, **kwargs)
            result = (await result) if is_awaitable(result) else result
            return (
                "call_success",
                {
                    "callback_id": callback_id,
                    "target_origin": data.get("origin"),
                    "value": result,
                },
            )
        except Exception as e:
            return (
                "call_failure",
                {
                    "callback_id": callback_id,
                    "target_origin": data.get("origin"),
                    "message": str(e),
                },
            )

    def handle_call_success_msg(self, data):
        if not self.message_targets_this_manager(data):
            return None

        callback_id, value = data["callback_id"], data.get("value")
        future = self.call_futures.pop(callback_id, None)
        if not future:
            return None
        future.set_result(value)

    def handle_call_failure_msg(self, data):
        if not self.message_targets_this_manager(data):
            return None

        callback_id, message = data["callback_id"], data["message"]
        future = self.call_futures.pop(callback_id, None)
        if not future:
            return None
        future.set_exception(Exception(message))

    def ensure_state_sync(self, sync_id):
        state_sync = self.state_sync.get(sync_id)
        if not state_sync:
            state_sync = {
                "status": "initialized",
                "pending_changes": [],
                "in_flight": None,
                "processor_running": False,
                "blocked_reason": None,
                "resync_request_id": None,
            }
            self.state_sync[sync_id] = state_sync
        return state_sync

    def set_state_sync_status(self, sync_id, status, reason=None):
        state_sync = self.ensure_state_sync(sync_id)
        state_sync["status"] = status
        state_sync["blocked_reason"] = reason
        return state_sync

    def get_state_sync_status(self, sync_id=None):
        if sync_id:
            state_sync = self.ensure_state_sync(sync_id)
            return {
                "status": state_sync["status"],
                "pending_count": len(state_sync["pending_changes"]),
                "in_flight": state_sync["in_flight"],
                "blocked_reason": state_sync["blocked_reason"],
            }
        result = {}
        for sid in self.state_sync.keys():
            result[sid] = self.get_state_sync_status(sid)
        return result

    def resume_state_sync_processors(self):
        for sync_id in tuple(self.state_sync.keys()):
            self.ensure_state_change_processor(sync_id)

    def rollback_state_changes(self, sync_id, count):
        if count <= 0:
            return 0
        state = self.states.get(sync_id)
        if not state:
            return 0
        self.disabled_state_sync.add(sync_id)
        try:
            return rollback_store_state(state, count)
        finally:
            self.disabled_state_sync.discard(sync_id)

    def block_state_sync(
        self,
        sync_id,
        reason,
        error=None,
        request_resync=True,
        update_connection=False,
    ):
        state_sync = self.set_state_sync_status(sync_id, "blocked", reason)
        if isinstance(error, Exception):
            exception = error
        elif error:
            exception = Exception(str(error))
        else:
            exception = Exception(reason)

        if update_connection:
            self.set_connection_status(
                connected=False,
                reason=reason,
                last_error=str(exception),
            )

        changes = []
        in_flight = state_sync["in_flight"]
        if in_flight:
            changes.append(in_flight)
        for change in state_sync["pending_changes"]:
            if change is not in_flight:
                changes.append(change)

        rollback_count = 0
        for change in changes:
            if not change["future"].done():
                change["future"].set_exception(exception)
            if change.get("rollbackable"):
                rollback_count += 1

        self.rollback_state_changes(sync_id, rollback_count)
        state_sync["pending_changes"] = []
        state_sync["in_flight"] = None
        state_sync["processor_running"] = False

        if request_resync:
            try:
                self.request_state_sync(sync_id)
            except Exception:
                pass

    def ensure_state_change_processor(self, sync_id):
        state_sync = self.ensure_state_sync(sync_id)
        if state_sync["processor_running"]:
            return
        if len(state_sync["pending_changes"]) == 0:
            if state_sync["status"] == "sent_change":
                self.set_state_sync_status(sync_id, "ready")
            return
        if state_sync["status"] in ("blocked", "resyncing"):
            return
        if self.connection_state.get("connected") is False:
            return
        state_sync["processor_running"] = True
        task = self.process_state_changes(sync_id)
        if is_awaitable(task):
            start_async_task(task)

    async def process_state_changes(self, sync_id):
        state_sync = self.ensure_state_sync(sync_id)
        try:
            while len(state_sync["pending_changes"]) > 0:
                if state_sync["status"] in ("blocked", "resyncing"):
                    return
                if self.connection_state.get("connected") is False:
                    return

                change = state_sync["pending_changes"][0]
                state_sync["in_flight"] = change
                self.set_state_sync_status(sync_id, "sent_change")

                def on_timeout():
                    current = state_sync["in_flight"]
                    if current is change and not change["future"].done():
                        self.block_state_sync(
                            sync_id,
                            "state_change_timeout",
                            Exception("State change timed out before it was acknowledged"),
                            request_resync=True,
                            update_connection=True,
                        )

                self.set_timeout(on_timeout, STATE_CHANGE_TIMEOUT)

                try:

                    def on_send_failure(error, method, data):
                        self.block_state_sync(
                            sync_id,
                            "state_change_send_failed",
                            error,
                            request_resync=True,
                            update_connection=True,
                        )

                    sent = self.send_outgoing_message(
                        "state_change",
                        change["payload"],
                        on_failure=on_send_failure,
                    )
                    if is_awaitable(sent):
                        await sent
                    await change["future"]
                except Exception as error:
                    if state_sync["status"] not in ("blocked", "resyncing"):
                        self.block_state_sync(
                            sync_id,
                            "state_change_failed",
                            error,
                            request_resync=True,
                        )
                    return

                if (
                    len(state_sync["pending_changes"]) > 0
                    and state_sync["pending_changes"][0] is change
                ):
                    state_sync["pending_changes"].pop(0)
                state_sync["in_flight"] = None

            self.set_state_sync_status(sync_id, "ready")
        finally:
            state_sync["processor_running"] = False
            if len(state_sync["pending_changes"]) > 0 and state_sync["status"] == "ready":
                self.ensure_state_change_processor(sync_id)

    def register_state(self, sync_id, doc: Any):
        self.states.__setitem__(sync_id, doc)
        self.set_state_sync_status(sync_id, "ready")
        ensure_store_undo_manager(doc)
        install_store_sync_guard(doc, lambda: self.assert_state_write_allowed(sync_id))
        self.states_subscriptions[doc] = doc.on_update(
            lambda update: self.send_state_change(update, sync_id=sync_id)
        )

    def apply_state_update(self, sync_id, state, update, origin):
        self.disabled_state_sync.add(sync_id)
        try:
            previous_origin = self.current_origin
            self.current_origin = origin if origin else previous_origin
            try:
                state.apply_update(update)
            finally:
                self.current_origin = previous_origin
        finally:
            self.disabled_state_sync.discard(sync_id)

    def send_state_change(self, update, sync_id):
        if sync_id in self.disabled_state_sync:
            return None
        state = self.states.get(sync_id)
        if not state:
            return None
        state_sync = self.ensure_state_sync(sync_id)
        state_change_id = make_uuid()
        payload = {
            "state_change_id": state_change_id,
            "update": b64_encode(update),
            "state_vector": b64_encode(state.get_state()),
            "sync_id": sync_id,
            "origin": self.current_origin,
        }
        state_change_future = Future()
        change = {
            "state_change_id": state_change_id,
            "payload": payload,
            "future": state_change_future,
            "rollbackable": payload["origin"] == self.uid,
        }
        state_sync["pending_changes"].append(change)
        self.ensure_state_change_processor(sync_id)
        return state_change_future

    def handle_state_change_msg(self, data):
        if data["origin"] == self.uid:
            return None
        update = b64_decode(data["update"])
        sync_id = data["sync_id"]
        state = self.states.get(sync_id)
        if not state:
            return None
        try:
            self.apply_state_update(sync_id, state, update, data["origin"])
        except Exception:
            return None
        return None

    def handle_state_change_result_msg(self, data):
        if not self.message_targets_this_manager(data):
            return None

        sync_id = data["sync_id"]
        state_sync = self.ensure_state_sync(sync_id)
        change = state_sync["in_flight"]
        if not change:
            return None
        future = change["future"]
        if change["state_change_id"] != data["state_change_id"]:
            return None
        if data["status"] == "failed":
            exception = Exception(data["message"])
            if not future.done():
                future.set_exception(exception)
        else:
            missing_update = data.get("missing_update")
            if missing_update:
                state = self.states.get(sync_id)
                if state:
                    self.apply_state_update(
                        sync_id,
                        state,
                        b64_decode(missing_update),
                        data.get("origin"),
                    )
            if not future.done():
                future.set_result(None)

    def set_timeout(self, callback, timeout):
        return schedule_timeout(callback, timeout)

    def register_function(self, fn, identifier=None) -> str:
        if not identifier:
            identifier = function_identifier(fn)
        self.functions.__setitem__(identifier, fn)  # small hack bc weakdict is tricky atm
        return identifier


class ServerManager:
    manager = None

    def __init__(self):
        self.functions = {}
        self.refs = {}
        self.states: "WeakValueDictionary[str, Any]" = WeakValueDictionary()
        self.states_subscriptions: "WeakKeyDictionary[Any, Any]" = WeakKeyDictionary()
        self.call_futures = {}
        self.uid = make_uuid()
        self.current_origin = self.uid
        self.register_function(self.call_ref_method, "<ref_method>")
        self.last_messages = []

    def send_message(self, method, data):
        raise NotImplementedError()

    def handle_message(self, method, data):
        self.last_messages.append([method, data])
        if method == "call":
            return self.handle_call_msg(data)
        elif method == "state_change":
            return self.handle_state_change_msg(data)
        elif method == "state_change_result":
            return self.handle_state_change_result_msg(data)
        elif method == "call_success":
            return self.handle_call_success_msg(data)
        elif method == "call_failure":
            return self.handle_call_failure_msg(data)
        elif method == "state_sync_request":
            return self.handle_state_sync_request_msg(data)
        elif method == "state_sync_response":
            return self.handle_state_sync_response_msg(data)
        elif method == "is_alive_request":
            return self.handle_is_alive_request(data)
        elif method == "bundle_request":
            return self.handle_bundle_request(data)
        else:
            raise Exception(f"Unknown method: {method}")

    def message_targets_this_manager(self, data):
        target_origin = data.get("target_origin") if data else None
        return not target_origin or target_origin == self.uid

    def register_ref(self, ref_id, ref):
        self.refs[ref_id] = ref

    def call_ref_method(self, ref_id, method_name, args, kwargs):
        ref = self.refs.get(ref_id)
        if not ref:
            print(f"Reference with id {ref_id} not found")
        if not ref.current:
            return None
        method = ref.current[method_name]
        return method(*args, **kwargs)

    def remote_call_ref_method(self, ref_id, method_name, args, kwargs):
        return self.send_call(
            "<ref_method>",
            (ref_id, method_name, args, kwargs),
            {},
        )

    def send_call(self, function_id, args, kwargs):
        callback_id = make_uuid()
        payload = {
            "function_id": function_id,
            "args": args,
            "kwargs": kwargs,
            "callback_id": callback_id,
            "origin": self.uid,
        }
        future = Future()
        self.register_call_future(callback_id, future)
        try:
            sent = self.send_message("call", payload)
        except Exception as error:
            self.call_futures.pop(callback_id, None)
            future.set_exception(error if isinstance(error, Exception) else Exception(str(error)))
            return future

        if is_awaitable(sent):

            async def await_sent():
                try:
                    await sent
                except Exception as error:
                    self.call_futures.pop(callback_id, None)
                    if not future.done():
                        future.set_exception(error)

            start_async_task(await_sent())
        return future

    def register_call_future(self, callback_id, future):
        self.call_futures[callback_id] = future

    def handle_call_success_msg(self, data):
        if not self.message_targets_this_manager(data):
            return None
        callback_id, value = data["callback_id"], data.get("value")
        future = self.call_futures.pop(callback_id, None)
        if not future:
            return None
        future.set_result(value)

    def handle_call_failure_msg(self, data):
        if not self.message_targets_this_manager(data):
            return None
        callback_id, message = data["callback_id"], data["message"]
        future = self.call_futures.pop(callback_id, None)
        if not future:
            return None
        future.set_exception(Exception(message))

    async def handle_call_msg(self, data):
        function_id, args, kwargs, callback_id = (
            data["function_id"],
            data["args"],
            data["kwargs"],
            data["callback_id"],
        )
        try:
            fn = self.functions.get(function_id)
            result = fn(*args, **kwargs)
            result = (await result) if is_awaitable(result) else result
            return (
                "call_success",
                {
                    "callback_id": callback_id,
                    "target_origin": data.get("origin"),
                    "value": result,
                },
            )
        except Exception as e:
            return (
                "call_failure",
                {
                    "callback_id": callback_id,
                    "target_origin": data.get("origin"),
                    "message": str(e),
                },
            )

    def handle_state_sync_request_msg(self, data=None):
        sync_id = data.get("sync_id") if data else None
        state_vectors = data.get("state_vectors", {}) if data else {}
        updates = {}
        response_state_vectors = {}
        for sid, state in self.states.items():
            if not sync_id or sid == sync_id:
                state_vector = state_vectors.get(sid)
                update = (
                    state.get_update(b64_decode(state_vector))
                    if state_vector
                    else state.get_update()
                )
                if len(update):
                    updates[sid] = b64_encode(update)
                response_state_vectors[sid] = b64_encode(state.get_state())
        payload = {
            "request_id": data.get("request_id") if data else None,
            "updates": updates,
            "state_vectors": response_state_vectors,
            "origin": self.uid,
            "target_origin": data.get("origin") if data else None,
        }
        if sync_id:
            payload["sync_id"] = sync_id
        return "state_sync_response", payload

    def handle_state_sync_response_msg(self, data=None):
        return None

    def handle_state_change_msg(self, data):
        update = b64_decode(data["update"])
        sync_id = data["sync_id"]
        state = self.states.get(sync_id)
        if not state:
            return (
                "state_change_result",
                {
                    "state_change_id": data.get("state_change_id"),
                    "sync_id": sync_id,
                    "target_origin": data.get("origin"),
                    "message": f"State {sync_id} is not registered",
                    "status": "failed",
                },
            )
        try:
            self.apply_state_update(state, update, data["origin"])
        except Exception as e:
            return (
                "state_change_result",
                {
                    "state_change_id": data["state_change_id"],
                    "sync_id": sync_id,
                    "target_origin": data.get("origin"),
                    "message": str(e),
                    "status": "failed",
                },
            )
        else:
            missing_update = None
            state_vector = data.get("state_vector")
            if state_vector:
                update = state.get_update(b64_decode(state_vector))
                if len(update):
                    missing_update = b64_encode(update)
            return (
                "state_change_result",
                {
                    "state_change_id": data["state_change_id"],
                    "sync_id": sync_id,
                    "origin": self.uid,
                    "target_origin": data.get("origin"),
                    "missing_update": missing_update,
                    "status": "success",
                },
            )

    def handle_state_change_result_msg(self, data):
        return None

    def register_state(self, sync_id, doc: Any):
        self.states.__setitem__(sync_id, doc)
        self.states_subscriptions[doc] = doc.on_update(
            lambda update: self.send_state_change(update, sync_id=sync_id)
        )

    def apply_state_update(self, state, update, origin):
        previous_origin = self.current_origin
        self.current_origin = origin if origin else previous_origin
        try:
            state.apply_update(update)
        finally:
            self.current_origin = previous_origin

    def send_state_change(self, update, sync_id):
        if sync_id not in self.states:
            return None
        payload = {
            "state_change_id": make_uuid(),
            "update": b64_encode(update),
            "sync_id": sync_id,
            "origin": self.current_origin,
        }
        return self.send_message("state_change", payload)

    def handle_is_alive_request(self, data):
        return (
            "is_alive_response",
            {
                "request_id": data.get("request_id"),
                "ok": True,
            },
        )

    def handle_bundle_request(self, data):
        raise NotImplementedError()

    def register_function(self, fn, identifier=None) -> str:
        if not identifier:
            identifier = function_identifier(fn)
        self.functions.__setitem__(identifier, fn)  # small hack bc weakdict is tricky atm
        return identifier

    def __reduce__(self):
        return get_manager, ()


@marshal_as(
    js="""
return (function b64_encode(data) {
    var u8 = new Uint8Array(data);
    var binary = '';
    for (var i = 0; i < u8.length; i += 32768) {
        binary += String.fromCharCode.apply(
          null,
          u8.subarray(i, i + 32768)
        );
    }
    return btoa(binary);
});
"""
)
def b64_encode(data: bytes) -> str:
    """Encode bytes to a base64 string."""
    return base64.b64encode(data).decode("utf-8")


@marshal_as(
    js="""
return (function b64_decode(data) {
    return Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
});
"""
)
def b64_decode(data: str) -> bytes:
    """Decode a base64 string to bytes."""
    return base64.b64decode(data.encode("utf-8"))


class JupyterClientManager(ClientManager):
    def __init__(self):
        super().__init__()
        self.env_handler = None
        self.connection_state["kind"] = "jupyter_client"
        self.connection_state["transport"] = "jupyter-comm"
        self.connection_state["connected"] = False
        self.connection_state["reason"] = "initializing"

    def register_environment_handler(self, handler):
        self.env_handler = handler

    async def send_message(self, method, data):
        if not self.env_handler:
            self.set_connection_status(
                connected=False,
                reason="missing_environment_handler",
                last_error="No environment handler set",
            )
            raise Exception("No environment handler set")
        if not self.connection_state["connected"]:
            raise Exception("Not connected")
        try:
            await self.env_handler.sendMessage(method, data)
        except BaseException as error:
            message = str(error)
            reason = "send_timeout" if "PRET_COMM_TIMEOUT" in message else "send_failed"
            self.set_connection_status(
                connected=False,
                reason=reason,
                last_error=message,
            )
            raise Exception(message)

    async def handle_comm_message(self, msg):
        """Called when a message is received from the front-end"""
        msg_content = msg["content"]["data"]
        if "method" not in msg_content:
            return None
        method = msg_content["method"]
        data = msg_content["data"]
        self.set_connection_status(
            connected=True,
            transport="jupyter-comm",
            reason="message_received",
            last_error=None,
        )

        result = self.handle_message(method, data)
        if result:
            # check awaitable, and send back message if resolved
            result = await result
            if result:
                send_future = self.send_outgoing_message(*result)
                if is_awaitable(send_future):
                    await send_future


@marshal_as(JupyterClientManager)
class JupyterServerManager(ServerManager):
    def __init__(self):
        super().__init__()
        self.comm = None
        self.open()

    def open(self):
        from ipykernel.comm import Comm

        """Open a comm to the frontend if one isn't already open."""
        if self.comm is None:
            # It seems that if we create a comm to early, the client might
            # receive the message before the "pret" comm target is registered
            # in the front-end.
            # This is why we also register the target in the constructor
            # since the comm creation might come from the front-end instead.
            comm = Comm(target_name="pret", data={})
            comm.on_msg(self.handle_comm_msg)
            self.comm = comm

            comm_manager = getattr(comm.kernel, "comm_manager", None)
            # LOG[0] += str(("comm_manager", comm_manager))
            if comm_manager is None:
                raise Exception("Could not find a comm_manager attached to the kernel")
            comm_manager.register_target("pret", self.handle_comm_open)

    def handle_comm_open(self, comm, msg):
        self.comm = comm
        self.comm.on_msg(self.handle_comm_msg)

    def close(self):
        """Close method.
        Closes the underlying comm.
        When the comm is closed, all the view views are automatically
        removed from the front-end."""
        if self.comm is not None:
            self.comm.close()
            self.comm = None

    def send_message(self, method, data, metadata=None, buffers=None):
        self.comm.send(
            {
                "method": method,
                "data": data,
            },
            metadata,
            buffers,
        )

    def __del__(self):
        self.close()

    async def await_and_send_message(self, result):
        if is_awaitable(result):
            result = await result
        if result:
            self.send_message(*result)

    @weakmethod
    def handle_comm_msg(self, msg):
        """Called when a message is received from the front-end"""
        msg_content = msg["content"]["data"]
        if "method" not in msg_content:
            return None
        method = msg_content["method"]
        data = msg_content["data"]

        result = self.handle_message(method, data)
        if result:
            # check awaitable, and send back message if resolved is not None
            result = self.await_and_send_message(result)
            if is_awaitable(result):
                start_async_task(result)

    def handle_bundle_request(self, data):
        request_id = data.get("request_id")
        marshaler_id = data.get("marshaler_id")
        byte_offset = data.get("byte_offset", 0)
        try:
            if not marshaler_id:
                raise Exception("marshaler_id is required")
            from pret.marshal import get_shared_marshaler

            marshaler = get_shared_marshaler()
            if not marshaler or marshaler.id != marshaler_id:
                raise Exception(
                    f"Marshaler {marshaler_id} not found in current session.\n"
                    f"You are likely trying to render an outdated version of the widget, and the "
                    f"kernel has changed since its info was stored. Remember to save (Ctrl-S) "
                    f"after you run a cell, and consider restarting the kernel and/or reloading the"
                    f"page."
                )
            blob, code = marshaler.get_serialized_bytes()
            bundle_suffix = memoryview(blob)[byte_offset:]
            compression = None
            if len(bundle_suffix) >= PRET_BUNDLE_GZIP_MIN_BYTES:
                bundle_suffix = gzip.compress(bundle_suffix)
                compression = "gzip"
            return (
                "bundle_response",
                {
                    "request_id": request_id,
                    "marshaler_id": marshaler_id,
                    "max_chunk_idx": marshaler.chunk_idx - 1,
                    "byte_offset": byte_offset,
                    "byte_length": len(blob),
                    "code": code,
                    "compression": compression,
                },
                None,
                [bundle_suffix],
            )
        except Exception as e:
            return (
                "bundle_response",
                {
                    "request_id": request_id,
                    "marshaler_id": marshaler_id,
                    "error": str(e),
                },
            )


@marshal_as(
    js="""
return function make_websocket(resource) {
    return new WebSocket(resource);
}
""",
    globals={},
)
def make_websocket(protocol: str = "ws") -> Any:
    raise NotImplementedError("This function is not meant to be called from Python. ")


class StandaloneClientManager(ClientManager):
    def __init__(self):
        super().__init__()
        self.connection_state["kind"] = "standalone_client"
        self.connection_state["transport"] = "standalone-http"
        self.connection_state["connected"] = False
        self.connection_state["reason"] = "initializing"
        self.websocket = make_websocket("/ws")

        def on_message(event):
            """Handle incoming messages from the WebSocket."""
            data = event.data
            data = loads(data)
            result = self.handle_message(data["method"], data["data"])
            if result:

                async def send_result():
                    resolved_result = await result if is_awaitable(result) else result
                    if resolved_result:
                        send_future = self.send_outgoing_message(*resolved_result)
                        if is_awaitable(send_future):
                            await send_future

                task = send_result()
                if is_awaitable(task):
                    start_async_task(task)

        def on_open(event):
            self.set_connection_status(
                connected=True,
                transport="websocket",
                reason="websocket_open",
                last_error=None,
            )
            self.request_state_sync()

        def on_close(event):
            self.set_connection_status(
                connected=False,
                transport="websocket",
                reason="websocket_closed",
            )

        def on_error(event):
            self.set_connection_status(
                connected=False,
                transport="websocket",
                reason="websocket_error",
            )

        # add a listener with cb to self.handle_message
        self.websocket.addEventListener("message", on_message)
        self.websocket.addEventListener("open", on_open)
        self.websocket.addEventListener("close", on_close)
        self.websocket.addEventListener("error", on_error)

    async def send_message(self, method, data):
        try:
            response = await fetch(
                "method",
                {
                    "method": "POST",
                    "body": dumps({"method": method, "data": data}),
                    "headers": {"Content-Type": "application/json"},
                },
            )
            if response.ok is False:
                raise Exception(f"Failed to POST method call: {response.status}")
            result = await response.json()
            if "method" in result and "data" in result:
                future = self.handle_message(result["method"], result["data"])
                if is_awaitable(future):
                    await future
            self.set_connection_status(
                connected=True,
                transport="standalone-http",
                reason="send_ok",
                last_error=None,
            )
        except BaseException as error:
            self.set_connection_status(
                connected=False,
                transport="standalone-http",
                reason="send_failed",
                last_error=str(error),
            )
            raise Exception("Could not communicate with server")


@marshal_as(StandaloneClientManager)
class StandaloneServerManager(ServerManager):
    def __init__(self):
        super().__init__()
        self.connections = {}
        self.origin_connections = {}

    def register_connection(self, connection_id):
        import asyncio

        queue = asyncio.Queue()
        self.connections[connection_id] = queue
        return queue

    def unregister_connection(self, connection_id):
        self.connections.pop(connection_id, None)
        for origin, mapped_connection_id in tuple(self.origin_connections.items()):
            if mapped_connection_id == connection_id:
                self.origin_connections.pop(origin, None)

    def send_message(self, method, data, connection_ids=None):
        if connection_ids is None:
            target_origin = data.get("target_origin") if data else None
            if target_origin and target_origin in self.origin_connections:
                connection_ids = [self.origin_connections[target_origin]]
            else:
                excluded_connection_id = None
                origin = data.get("origin") if data else None
                if method == "state_change" and origin:
                    excluded_connection_id = self.origin_connections.get(origin)
                connection_ids = [
                    connection_id
                    for connection_id in self.connections.keys()
                    if connection_id != excluded_connection_id
                ]
        for connection_id in connection_ids:
            self.connections[connection_id].put_nowait({"method": method, "data": data})

    async def handle_websocket_msg(self, data, connection_id):
        origin = data.get("data", {}).get("origin")
        if origin:
            self.origin_connections[origin] = connection_id
        result = self.handle_message(data["method"], data["data"])
        if result is not None:
            if is_awaitable(result):
                result = await result
            self.send_message(*result, connection_ids=[connection_id])


def check_jupyter_environment():
    try:
        from IPython import get_ipython

        if get_ipython() is not None:
            return True
    except ImportError:
        pass

    return False


def make_get_manager() -> Callable[[], Union[ClientManager, ServerManager]]:
    def get_jupyter_client_manager():
        if JupyterClientManager.manager is None:
            JupyterClientManager.manager = JupyterClientManager()

        return JupyterClientManager.manager

    @marshal_as(get_jupyter_client_manager)
    def get_jupyter_server_manager():
        if JupyterServerManager.manager is None:
            JupyterServerManager.manager = JupyterServerManager()

        return JupyterServerManager.manager

    def get_standalone_client_manager():
        if StandaloneClientManager.manager is None:
            StandaloneClientManager.manager = StandaloneClientManager()

        return StandaloneClientManager.manager

    @marshal_as(get_standalone_client_manager)
    def get_standalone_server_manager():
        if StandaloneServerManager.manager is None:
            StandaloneServerManager.manager = StandaloneServerManager()

        return StandaloneServerManager.manager

    # check if we are in a jupyter environment
    if check_jupyter_environment():
        return get_jupyter_server_manager
    else:
        return get_standalone_server_manager


get_manager = make_get_manager()
