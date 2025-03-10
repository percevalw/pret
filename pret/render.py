import base64
import functools
import inspect
from types import FunctionType
from typing import Callable, TypeVar

from pret.bridge import auto_start_async, create_proxy, js, pyodide
from pret.manager import get_manager
from pret.serialize import PretPickler, get_shared_pickler, pickle_as

T = TypeVar("T")


def create_element(element_type, props, children):
    children = (
        [
            pyodide.ffi.to_js(child, dict_converter=js.Object.fromEntries)
            for child in children
        ]
        if isinstance(children, list)
        else pyodide.ffi.to_js(children, dict_converter=js.Object.fromEntries)
    )
    result = js.React.createElement(
        # element_type is either
        # - str -> str
        # - or py function -> PyProxy
        element_type,
        props,
        *[
            pyodide.ffi.to_js(child, dict_converter=js.Object.fromEntries)
            for child in children
        ],
    )
    return result


def make_create_element_from_function(fn, with_proxy=False):
    @create_proxy
    def react_type_fn(props, ctx=None):
        if isinstance(props, pyodide.ffi.JsProxy):
            props = props.to_py(depth=1)
        # weird bug where children is a dict instead of a list, introduced between
        # pyodide 0.23.2 and pyodide 0.26.2
        children = props.pop("children").values() if "children" in props else []
        return fn(*children, **props)

    def create(*children, **props):
        return js.React.createElement(
            # Element_type is a py function -> PyProxy
            react_type_fn,
            # Passed props is a JsProxy of a JS object.
            # Since element_type is a py function, props will come back in Python when
            # React calls it, so it's a shallow conversion (depth=1). We could have done
            # no pydict -> jsobject conversion (and send a PyProxy) but React expect an
            # object
            pyodide.ffi.to_js(props, depth=1, dict_converter=js.Object.fromEntries),
            # Same, this proxy will be converted back to the original Python object
            # when React calls the function, but this time we don't need to convert it
            # to a JS Array (React will pass it as is)
            pyodide.ffi.create_proxy(children),
        )

    return create


def wrap_prop(key, prop):
    if isinstance(prop, FunctionType):

        def wrapped(*args, **kwargs):
            args = [
                arg.to_py() if isinstance(arg, pyodide.ffi.JsProxy) else arg
                for arg in args
            ]
            kwargs = {
                kw: arg.to_py() if isinstance(arg, pyodide.ffi.JsProxy) else arg
                for kw, arg in kwargs
            }
            return prop(*args, **kwargs)

        if inspect.iscoroutinefunction(prop):
            return auto_start_async(wrapped)

        return wrapped
    else:
        return prop


def stub_component(name, props_mapping) -> Callable[[T], T]:
    def make(fn):
        def create_fn(*children, **props):
            return js.React.createElement(
                # element_type is either a str or a PyProxy of a JS function
                # such as window.JoyUI.Button
                name,
                # Deep convert to JS objects (depth=-1) to avoid issues with React
                pyodide.ffi.to_js(
                    {
                        props_mapping.get(k, k): wrap_prop(k, v)
                        for k, v in props.items()
                    },
                    dict_converter=js.Object.fromEntries,
                    depth=-1,
                ),
                # Deep convert too, same reason
                *(pyodide.ffi.to_js(child) for child in children),
            )

        @functools.wraps(fn)
        @pickle_as(create_fn)
        def wrapped(*children, detach=False, **props):
            def render():
                return create_fn(*children, **props)

            return Renderable(
                render,
                detach=detach,
            )

        return wrapped

    return make


def component(fn):
    create_fn = make_create_element_from_function(fn)

    @functools.wraps(fn)
    @pickle_as(create_fn)
    def wrapped(*children, detach=False, **props):
        def render():
            return create_fn(*children, **props)

        return Renderable(
            render,
            detach=detach,
        )

    return wrapped


class ClientRef:
    registry = {}

    def __init__(self, id):
        self.id = id
        self.current = None
        ClientRef.registry[id] = self

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.__dict__["current"] = None
        ClientRef.registry[self.id] = self

    def __call__(self, element):
        self.current = element

    def __repr__(self):
        return f"Ref(id={self.id}, current={repr(self.current)})"


@pickle_as(ClientRef)
class Ref:
    registry = {}

    def __init__(self, id):
        self.id = id

    def _remote_call(self, attr, *args, **kwargs):
        return get_manager().remote_call(attr, args, kwargs)

    def __getattr__(self, attr):
        return functools.partial(self._remote_call, attr)


class Renderable:
    def __init__(self, dillable, detach):
        self.dillable = dillable
        self.detach = detach
        self.pickler = None
        self.data = None

    def ensure_pickler(self) -> PretPickler:
        # Not in __init__ to allow a previous overwritten view
        # to be deleted and garbage collected
        if self.pickler is None:
            import gc

            gc.collect()
            self.pickler = get_shared_pickler()
        return self.pickler

    def bundle(self):
        data, chunk_idx = self.ensure_pickler().dump((self.dillable, get_manager()))
        return base64.encodebytes(data).decode(), chunk_idx

    def _repr_mimebundle_(self, *args, **kwargs):
        plaintext = repr(self)
        if len(plaintext) > 110:
            plaintext = plaintext[:110] + "…"
        data, chunk_idx = self.bundle()
        return {
            "text/plain": plaintext,
            "application/vnd.pret+json": {
                "detach": self.detach,
                "version_major": 0,
                "version_minor": 0,
                "view_data": {
                    "unpickler_id": self.pickler.id,
                    "serialized": data,
                    "chunk_idx": chunk_idx,
                },
            },
        }


def make_remote_callable(function_id):
    async def remote_call(*args, **kwargs):
        return await get_manager().send_call(function_id, *args, **kwargs)

    return remote_call


def server_only(fn):
    return pickle_as(fn, make_remote_callable(get_manager().register_function(fn)))
