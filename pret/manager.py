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
    module_name = module.__name__
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
return () => crypto.randomUUID();
"""
)
def make_uuid():
    return uuid.uuid4().hex


class Manager:
    manager = None

    def __init__(self):
        # Could we simplify this by having one dict: sync_id -> (state, unsubscribe) ?
        # This would require making a custom WeakValueDictionary that can watch
        # the content of the value tuples
        self.functions = WeakValueDictionary()
        self.states: "WeakValueDictionary[str, Any]" = WeakValueDictionary()
        self.states_subscriptions: "WeakKeyDictionary[Any, Any]" = WeakKeyDictionary()
        self.call_futures = {}
        self.disabled_state_sync = set()
        self.uid = make_uuid()
        self._current_origin = self.uid

    def send_message(self, method, data):
        raise NotImplementedError()

    def handle_message(self, method, data):
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
        else:
            raise Exception(f"Unknown method: {method}")

    async def handle_call_msg(self, data):
        function_id, args, kwargs, callback_id = (
            data["function_id"],
            data["args"],
            data["kwargs"],
            data["callback_id"],
        )
        try:
            fn = self.functions[function_id]
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
        callback_id, value = data["callback_id"], data["value"]
        future = self.call_futures.pop(callback_id, None)
        if future is None:
            return
        future.set_result(value)

    def handle_call_failure_msg(self, data):
        callback_id, message = data["callback_id"], data["message"]
        future = self.call_futures.pop(callback_id, None)
        if future is None:
            return
        future.set_exception(Exception(message))

    def handle_state_sync_request_msg(self, sync_id=None):
        for sid, state in self.states.items():
            if sync_id is None or sid == sync_id:
                self.send_state_change(state.get_update(), sid)

    def send_call(self, function_id, args, kwargs):
        callback_id = make_uuid()
        message_future = self.send_message(
            "call",
            {
                "function_id": function_id,
                "args": args,
                "kwargs": kwargs,
                "callback_id": callback_id,
            },
        )
        if is_awaitable(message_future):
            start_async_task(message_future)
        future = Future()
        self.register_call_future(callback_id, future)
        return future

    def register_call_future(self, callback_id, future):
        self.call_futures[callback_id] = future

    def register_state(self, sync_id, doc: Any):
        self.states[sync_id] = doc
        self.states_subscriptions[doc] = doc.on_update(
            lambda update: self.send_state_change(update, sync_id=sync_id)
        )

    def handle_state_change_msg(self, data):
        if data["origin"] == self.uid:
            return
        update = b64_decode(data["update"])
        state = self.states[data["sync_id"]]
        self._current_origin = data["origin"]
        state.apply_update(update)
        self._current_origin = self.uid

    def send_state_change(self, update, sync_id):
        self.send_message(
            "state_change",
            {
                "update": b64_encode(update),
                "sync_id": sync_id,
                "origin": self._current_origin,
            },
        )

    def register_function(self, fn) -> str:
        identifier = function_identifier(fn)
        self.functions[identifier] = fn
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

    def register_environment_handler(self, handler):
        self.env_handler = handler
        self.send_message("state_sync_request", {})

    def send_message(self, method, data):
        if self.env_handler is None:
            raise Exception("No environment handler set")
        self.env_handler.sendMessage(method, data)


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
            return
        method = msg_content["method"]
        data = msg_content["data"]

        result = self.handle_message(method, data)
        if result is not None:
            # check awaitable, and send back message if resolved is not None
            result = self._await_and_send_message(result)
            if is_awaitable(result):
                start_async_task(result)


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
        self.websocket = make_websocket("/ws")

        def on_message(event):
            """Handle incoming messages from the WebSocket."""
            data = event.data
            data = loads(data)
            self.handle_message(data["method"], data["data"])

        # add a listener with cb to self.handle_message
        self.websocket.addEventListener("message", on_message)
        self.websocket.addEventListener(
            "open", lambda: self.send_message("state_sync_request", {})
        )

    async def send_message(self, method, data):
        response = await fetch(
            "method",
            {
                "method": "POST",
                "body": dumps({"method": method, "data": data}),
                "headers": {"Content-Type": "application/json"},
            },
        )
        result = await response.json()
        if "method" in result and "data" in result:
            future = self.handle_message(result["method"], result["data"])
            if is_awaitable(future):
                await future


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
