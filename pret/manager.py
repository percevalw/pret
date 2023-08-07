import asyncio
import hashlib
import inspect
import json
import linecache
import sys
import traceback
import uuid
from asyncio import Future
from types import FunctionType
from typing import Any, Callable
from weakref import WeakKeyDictionary, WeakValueDictionary, ref

from ipykernel.comm import Comm

from pret.bridge import js, pyodide
from pret.serialize import pickle_as

val = None


def get_formatted_exception():
    try:
        global val
        exc_type, exc_obj, tb = sys.exc_info()
        f = tb.tb_frame
        lineno = tb.tb_lineno
        filename = f.f_code.co_filename
        linecache.checkcache(filename)
        line = linecache.getline(filename, lineno, f.f_globals)
        val = traceback.format_exc()
        return f'Exception: {exc_obj}\nin "{line.strip()}"\nat {filename}:{lineno}'
    except Exception as e:
        return f"Exception: {e}"


def function_identifier(func: FunctionType):
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


class Manager:
    def __init__(self):
        # Could we simplify this by having one dict: sync_id -> (state, unsubscribe) ?
        # This would require making a custom WeakValueDictionary that can watch
        # the content of the value tuples
        self.functions = WeakValueDictionary()
        self.states: WeakValueDictionary[str, Any] = WeakValueDictionary()
        self.states_unsubcribe: WeakKeyDictionary[Any, Callable] = WeakKeyDictionary()
        self.call_futures = {}
        self.disabled_state_sync = set()

    def send_message(self, method, data):
        raise NotImplementedError()

    def handle_message(self, method, data):
        if method == "call":
            return self.handle_call(**data)
        elif method == "state_change":
            return self.handle_state_change(**data)
        elif method == "call_success":
            return self.handle_call_success(**data)
        elif method == "call_failure":
            return self.handle_call_failure(**data)
        else:
            raise Exception(f"Unknown method: {method}")

    async def handle_call(self, function_id, args, kwargs, callback_id):
        try:
            fn = self.functions[function_id]
            # check coroutine or sync function
            if inspect.iscoroutinefunction(fn):
                result = await fn(*args, **kwargs)
            else:
                result = fn(*args, **kwargs)

            return (
                "call_success",
                {
                    "callback_id": callback_id,
                    "value": result,
                },
            )
        except Exception:
            return (
                "call_failure",
                {
                    "callback_id": callback_id,
                    "message": get_formatted_exception(),
                },
            )

    def handle_call_success(self, callback_id, value):
        future = self.call_futures.pop(callback_id, None)
        if future is None:
            return
        future.set_result(value)

    def handle_call_failure(self, callback_id, message):
        future = self.call_futures.pop(callback_id, None)
        if future is None:
            return
        future.set_exception(Exception(message))

    def send_call(self, function_id, *args, **kwargs):
        try:
            callback_id = str(uuid.uuid4())
            message_future = self.send_message(
                "call",
                {
                    "function_id": function_id,
                    "args": args,
                    "kwargs": kwargs,
                    "callback_id": callback_id,
                },
            )
            if inspect.isawaitable(message_future):
                asyncio.create_task(message_future)
            future = Future()
            self.register_call_future(callback_id, future)
            return future
        except Exception:
            traceback.print_exc()

    def register_call_future(self, callback_id, future):
        self.call_futures[callback_id] = future

    def register_state(self, sync_id, state, unsubscribe):
        self.states[sync_id] = state
        self.states_unsubcribe[state] = unsubscribe

    def handle_state_change(self, ops, sync_id):
        state = self.states[sync_id]
        resubscribe = self.states_unsubcribe[state]()
        self.states[sync_id]._patch(ops)
        self.states_unsubcribe[state] = resubscribe()

    def send_state_change(self, ops, sync_id):
        self.send_message(method="state_change", data={"ops": ops, "sync_id": sync_id})

    def register_function(self, function: FunctionType) -> str:
        identifier = function_identifier(function)
        self.functions[identifier] = function
        return identifier


# noinspection PyUnboundLocalVariable
class JupyterServerManager(Manager):
    def __init__(self):
        super(JupyterServerManager, self).__init__()
        self.comm = None
        self.open()

    def open(self):
        """Open a comm to the frontend if one isn't already open."""
        if self.comm is None:
            comm = Comm(
                target_name="pret",
                data={},
            )
            comm.on_msg(self.handle_comm_msg)
            self.comm = comm

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
        return JupyterClientManager, ()

    async def send_awaitable_message(self, awaitable):
        result = await awaitable
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
            if inspect.isawaitable(result):
                asyncio.create_task(self.send_awaitable_message(result))


class JupyterClientManager(Manager):
    def __init__(self):
        super().__init__()
        self.env_handler = None

    def register_environment_handler(self, handler):
        self.env_handler = handler

    def send_message(self, method, data):
        if self.env_handler is None:
            raise Exception("No environment handler set")
        self.env_handler.sendMessage(
            method, pyodide.ffi.to_js(data, dict_converter=js.Object.fromEntries)
        )


class StandaloneServerManager(Manager):
    def __init__(self):
        super().__init__()
        self.connections = {}
        self._dillable = StandaloneClientManager()

    # def __reduce__(self):
    #     return StandaloneClientManager, ()

    def register_connection(self, connection_id):
        queue = asyncio.Queue()
        self.connections[connection_id] = queue
        return queue

    def unregister_connection(self, connection_id):
        self.connections.pop(connection_id)

    def send_message(self, method, data, connection_ids=None):
        if connection_ids is None:
            connection_ids = self.connections.keys()
        for connection_id in connection_ids:
            self.connections[connection_id].put_nowait((method, data))

    async def handle_websocket_msg(self, data, connection_id):
        result = self.handle_message(data["method"], data["data"])
        if result is not None:
            if inspect.isawaitable(result):
                result = await result
            self.send_message(*result, connection_ids=[connection_id])


class StandaloneClientManager(Manager):
    def __init__(self):
        super(StandaloneClientManager, self).__init__()

    async def send_message(self, method, data):
        response = await pyodide.http.pyfetch(
            "method",
            method="POST",
            body=json.dumps({"method": method, "data": data}),
            headers={"Content-Type": "application/json"},
        )
        result = await response.json()
        if "method" in result and "data" in result:
            future = self.handle_message(result["method"], result["data"])
            if inspect.isawaitable(future):
                await future


def check_jupyter_environment():
    try:
        from IPython import get_ipython

        if get_ipython() is not None:
            return True
    except ImportError:
        pass

    return False


def make_get_manager() -> Callable[[], Manager]:
    manager = None

    def get_jupyter_client_manager():
        nonlocal manager
        if manager is None:
            manager = JupyterClientManager()

        return manager

    @pickle_as(get_jupyter_client_manager)
    def get_jupyter_server_manager():
        nonlocal manager
        if manager is None:
            manager = JupyterServerManager()

        return manager

    def get_standalone_client_manager():
        nonlocal manager
        if manager is None:
            manager = StandaloneClientManager()

        return manager

    @pickle_as(get_standalone_client_manager)
    def get_standalone_server_manager():
        nonlocal manager
        if manager is None:
            manager = StandaloneServerManager()

        return manager

    # check if we are in a jupyter environment
    if check_jupyter_environment():
        return get_jupyter_server_manager
    else:
        return get_standalone_server_manager


get_manager = make_get_manager()
