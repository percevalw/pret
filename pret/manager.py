"""
This module provides client and server managers for handling remote calls,
state synchronization, and communication between the frontend and backend.
"""

import asyncio
import base64
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

    asyncio.create_task(task)


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
return function rollback_store_state(state) {
    if (window.storeLib && typeof window.storeLib.undoDoc === "function") {
        return window.storeLib.undoDoc(state);
    }
    return false;
}
""",
    globals={},
)
def rollback_store_state(state):
    return False


class Manager:
    manager = None

    def __init__(self):
        # Could we simplify this by having one dict: sync_id -> (state, unsubscribe) ?
        # This would require making a custom WeakValueDictionary that can watch
        # the content of the value tuples
        self.functions = {}
        self.refs = {}
        self.states: "WeakValueDictionary[str, Any]" = WeakValueDictionary()
        self.states_subscriptions: "WeakKeyDictionary[Any, Any]" = WeakKeyDictionary()
        self.call_futures = {}
        self.disabled_state_sync = set()
        self.message_queue_enabled = False
        self.outgoing_messages = []
        self._is_draining_outgoing_messages = False
        self.connection_state = {
            "kind": "unknown",
            "transport": None,
            "connected": None,
            "reason": None,
            "kernel_status": None,
            "kernel_connection_status": None,
            "last_error": None,
        }
        self._connection_state_listeners = set()
        self.uid = make_uuid()
        self._current_origin = self.uid
        self.register_function(self.call_ref_method, "<ref_method>")
        self.last_messages = []

    def send_message(self, method, data):
        raise NotImplementedError()

    def set_connection_status(
        self,
        connected=UNSET,
        reason=UNSET,
        transport=UNSET,
        kernel_status=UNSET,
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
        if kernel_status is not UNSET and kernel_status != current_state["kernel_status"]:
            new_state["kernel_status"] = kernel_status
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
            self._notify_connection_status_listeners()
        return self.connection_state

    def get_connection_status(self):
        return self.connection_state

    def _notify_connection_status_listeners(self):
        for callback in tuple(self._connection_state_listeners):
            try:
                callback()
            except Exception:
                pass

    def subscribe_connection_status(self, callback):
        self._connection_state_listeners.add(callback)

        def unsubscribe():
            self._connection_state_listeners.discard(callback)

        return unsubscribe

    def register_ref(self, ref_id, ref):
        self.refs[ref_id] = ref

    def call_ref_method(self, ref_id, method_name, args, kwargs):
        ref = self.refs.get(ref_id)
        if ref is None:
            print(f"Reference with id {ref_id} not found")
        if ref.current is None:
            return None
        method = ref.current[method_name]
        return method(*args, **kwargs)

    def remote_call_ref_method(self, ref_id, method_name, args, kwargs):
        return self.send_call(
            "<ref_method>",
            (ref_id, method_name, args, kwargs),
            {},
        )

    def handle_message(self, method, data):
        self.last_messages.append([method, data])
        if method == "call":
            return self.handle_call_msg(data)
        elif method == "state_change":
            return self.handle_state_change_msg(data)
        elif method == "call_success":
            return self.handle_call_success_msg(data)
        elif method == "call_failure":
            return self.handle_call_failure_msg(data)
        elif method == "state_sync_request":
            return self.handle_state_sync_request_msg(data.get("sync_id"))
        elif method == "is_alive_request":
            return self.handle_is_alive_request(data)
        elif method == "is_alive_response":
            return None
        elif method == "bundle_request":
            return self.handle_bundle_request(data)
        elif method == "bundle_response":
            return None
        else:
            raise Exception(f"Unknown method: {method}")

    def handle_bundle_request(self):
        raise NotImplementedError()

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
            return "call_success", {"callback_id": callback_id, "value": result}
        except Exception as e:
            return (
                "call_failure",
                {
                    "callback_id": callback_id,
                    "message": str(e),
                },
            )

    def handle_call_success_msg(self, data):
        callback_id, value = data["callback_id"], data.get("value")
        future = self.call_futures.pop(callback_id, None)
        if future is None:
            return None
        future.set_result(value)

    def handle_call_failure_msg(self, data):
        callback_id, message = data["callback_id"], data["message"]
        future = self.call_futures.pop(callback_id, None)
        if future is None:
            return None
        future.set_exception(Exception(message))

    def handle_state_sync_request_msg(self, sync_id=None):
        for sid, state in self.states.items():
            if sync_id is None or sid == sync_id:
                self.send_state_change(state.get_update(), sid)

    def handle_is_alive_request(self, data):
        return (
            "is_alive_response",
            {
                "request_id": data.get("request_id"),
                "ok": True,
            },
        )

    def send_call(self, function_id, args, kwargs):
        callback_id = make_uuid()
        payload = {
            "function_id": function_id,
            "args": args,
            "kwargs": kwargs,
            "callback_id": callback_id,
        }
        future = Future()
        self.register_call_future(callback_id, future)

        def on_send_failure(error, message):
            self._fail_call_future(callback_id, error)
            self._handle_outgoing_send_failure(message.get("method"), message.get("data"), error)

        self._send_outgoing_message("call", payload, on_failure=on_send_failure)
        return future

    def register_call_future(self, callback_id, future):
        self.call_futures[callback_id] = future

    def _fail_call_future(self, callback_id, error):
        future = self.call_futures.pop(callback_id, None)
        if future is None:
            return
        if isinstance(error, Exception):
            future.set_exception(error)
        else:
            future.set_exception(Exception(str(error)))

    def _handle_outgoing_send_failure(self, method, data, error):
        message = str(error)
        reason = "send_timeout" if "PRET_COMM_TIMEOUT" in message else "send_failed"
        self.set_connection_status(
            connected=False,
            reason=reason,
            last_error=message,
        )

    def _enqueue_outgoing_message(self, method, data, on_failure=None):
        future = Future()
        self.outgoing_messages.append(
            {
                "method": method,
                "data": data,
                "future": future,
                "on_failure": on_failure,
            }
        )
        self._ensure_outgoing_processor()
        return future

    def _send_outgoing_message(self, method, data, on_failure=None):
        if self.message_queue_enabled:
            return self._enqueue_outgoing_message(method, data, on_failure=on_failure)
        future = Future()
        message = {"method": method, "data": data}
        try:
            sent = self.send_message(method, data)
        except Exception as error:
            if on_failure is not None:
                on_failure(error, message)
            if isinstance(error, Exception):
                future.set_exception(error)
            else:
                future.set_exception(Exception(str(error)))
            return future

        if is_awaitable(sent):

            async def await_sent():
                try:
                    await sent
                    future.set_result(None)
                except Exception as error:
                    if on_failure is not None:
                        on_failure(error, message)
                    if isinstance(error, Exception):
                        future.set_exception(error)
                    else:
                        future.set_exception(Exception(str(error)))

            start_async_task(await_sent())
        else:
            future.set_result(None)
        return future

    def _ensure_outgoing_processor(self):
        if self._is_draining_outgoing_messages:
            return
        self._is_draining_outgoing_messages = True
        task = self._drain_outgoing_messages()
        if is_awaitable(task):
            start_async_task(task)

    async def _drain_outgoing_messages(self):
        try:
            while len(self.outgoing_messages):
                queued = self.outgoing_messages.pop(0)
                method = queued["method"]
                data = queued["data"]
                future = queued["future"]
                on_failure = queued.get("on_failure")
                message = {"method": method, "data": data}
                try:
                    sent = self.send_message(method, data)
                    if is_awaitable(sent):
                        await sent
                    future.set_result(None)
                except Exception as error:
                    self._handle_outgoing_send_failure(method, data, error)
                    try:
                        if on_failure is not None:
                            on_failure(error, message)
                    finally:
                        if isinstance(error, Exception):
                            future.set_exception(error)
                        else:
                            future.set_exception(Exception(str(error)))
        finally:
            self._is_draining_outgoing_messages = False
            if len(self.outgoing_messages):
                self._ensure_outgoing_processor()

    def _rollback_state(self, sync_id):
        state = self.states.get(sync_id)
        if state is None:
            return False
        self.disabled_state_sync.add(sync_id)
        try:
            return rollback_store_state(state)
        finally:
            self.disabled_state_sync.discard(sync_id)

    def register_state(self, sync_id, doc: Any):
        self.states.__setitem__(sync_id, doc)
        ensure_store_undo_manager(doc)
        self.states_subscriptions[doc] = doc.on_update(
            lambda update: self.send_state_change(update, sync_id=sync_id)
        )

    def handle_state_change_msg(self, data):
        if data["origin"] == self.uid:
            return None
        update = b64_decode(data["update"])
        state = self.states.get(data["sync_id"])
        self._current_origin = data["origin"]
        state.apply_update(update)
        self._current_origin = self.uid

    def send_state_change(self, update, sync_id):
        if sync_id in self.disabled_state_sync:
            return None
        payload = {
            "update": b64_encode(update),
            "sync_id": sync_id,
            "origin": self._current_origin,
        }

        def on_send_failure(error, message):
            self._rollback_state(sync_id)

        return self._send_outgoing_message(
            "state_change",
            payload,
            on_failure=on_send_failure,
        )

    def register_function(self, fn, identifier=None) -> str:
        if identifier is None:
            identifier = function_identifier(fn)
        self.functions.__setitem__(identifier, fn)  # small hack bc weakdict is tricky atm
        return identifier


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


class JupyterClientManager(Manager):
    def __init__(self):
        super().__init__()
        self.env_handler = None
        self.message_queue_enabled = True
        self.connection_state["kind"] = "jupyter_client"
        self.connection_state["transport"] = "jupyter-comm"
        self.connection_state["connected"] = False
        self.connection_state["reason"] = "initializing"

    def register_environment_handler(self, handler):
        self.env_handler = handler
        self._send_outgoing_message("state_sync_request", {})

    async def send_message(self, method, data):
        if self.env_handler is None:
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
            # check awaitable, and send back message if resolved is not None
            result = await result
            if result:
                send_future = self._send_outgoing_message(*result)
                if is_awaitable(send_future):
                    await send_future


@marshal_as(JupyterClientManager)
class JupyterServerManager(Manager):
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

    def send_message(self, method, data, metadata=None):
        self.comm.send(
            {
                "method": method,
                "data": data,
            },
            metadata,
        )

    def __del__(self):
        self.close()

    def __reduce__(self):
        return get_manager, ()

    async def _await_and_send_message(self, result):
        if is_awaitable(result):
            result = await result
        if result is not None:
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
        if result is not None:
            # check awaitable, and send back message if resolved is not None
            result = self._await_and_send_message(result)
            if is_awaitable(result):
                start_async_task(result)

    def handle_bundle_request(self, data):
        request_id = data.get("request_id")
        marshaler_id = data.get("marshaler_id")
        try:
            if marshaler_id is None:
                raise Exception("marshaler_id is required")
            from pret.marshal import get_shared_marshaler

            marshaler = get_shared_marshaler()
            if marshaler is None or marshaler.id != marshaler_id:
                raise Exception(
                    f"Marshaler {marshaler_id} not found in current session.\n"
                    f"You are likely trying to render an outdated version of the widget, and the "
                    f"kernel has changed since its info was stored. Remember to save (Ctrl-S) "
                    f"after you run a cell, and consider restarting the kernel and/or reloading the"
                    f"page."
                )
            serialized = marshaler.get_serialized()
            return (
                "bundle_response",
                {
                    "request_id": request_id,
                    "marshaler_id": marshaler_id,
                    "max_chunk_idx": marshaler.chunk_idx - 1,
                    "serialized": serialized,
                },
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


class StandaloneClientManager(Manager):
    def __init__(self):
        super().__init__()
        self.message_queue_enabled = True
        self.connection_state["kind"] = "standalone_client"
        self.connection_state["transport"] = "standalone-http"
        self.connection_state["connected"] = False
        self.connection_state["reason"] = "initializing"
        self.websocket = make_websocket("/ws")

        def on_message(event):
            """Handle incoming messages from the WebSocket."""
            data = event.data
            data = loads(data)
            self.handle_message(data["method"], data["data"])

        def on_open(event):
            self.set_connection_status(
                connected=True,
                transport="websocket",
                reason="websocket_open",
                last_error=None,
            )
            self._send_outgoing_message("state_sync_request", {})

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
class StandaloneServerManager(Manager):
    def __init__(self):
        super().__init__()
        self.connections = {}

    def __reduce__(self):
        return get_manager, ()

    def register_connection(self, connection_id):
        import asyncio

        queue = asyncio.Queue()
        self.connections[connection_id] = queue
        return queue

    def unregister_connection(self, connection_id):
        self.connections.pop(connection_id, None)

    def send_message(self, method, data, connection_ids=None):
        if connection_ids is None:
            connection_ids = self.connections.keys()
        for connection_id in connection_ids:
            self.connections[connection_id].put_nowait({"method": method, "data": data})

    async def handle_websocket_msg(self, data, connection_id):
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


def make_get_manager() -> Callable[[], Manager]:
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
