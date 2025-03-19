import base64
import functools
import inspect
from types import FunctionType
from typing import Awaitable, Callable, TypeVar, Union

from typing_extensions import ParamSpec

from pret.bridge import (
    auto_start_async,
    cached_create_proxy,
    cached_deep_to_js,
    create_proxy,
    js,
    pyodide,
    weak_cached,
)
from pret.manager import get_manager
from pret.serialize import PretPickler, get_shared_pickler, pickle_as
from pret.state import is_proxy

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


def make_create_element_from_function(fn):
    """
    Turn a Python Pret function into function that creates a React element.

    Parameters
    ----------
    fn: Callable
        The Python function to turn into a React element creator, ie a function
        that when invoked by React, will call the Python function with the
        correct arguments.

    Returns
    -------
    (**props) -> ReactElement<fn
    """

    @create_proxy
    def react_type_fn(props, ctx=None):
        props = props.to_py(depth=1)
        props = {
            key: value.unwrap()
            if isinstance(value, pyodide.ffi.JsDoubleProxy)
            else value
            for key, value in props.items()
        }
        # pyodide 0.23.2 and pyodide 0.26.2
        children = props.pop("children").values() if "children" in props else []
        return fn(*children, **props)

    name = fn.__name__

    def wrap_in_browser(react_fn):
        res = js.React.memo(react_fn)
        res.displayName = name
        return res

    react_type_fn = create_proxy(react_type_fn, wrap_in_browser=wrap_in_browser)

    def create(*children, **props):
        props_as_list = [
            [
                k,
                cached_create_proxy(v),
            ]
            for k, v in props.items()
        ]
        return js.React.createElement(
            # Element_type is a py function -> PyProxy
            react_type_fn,
            # Passed props is a JsProxy of a JS object.
            # Since element_type is a py function, props will come back in Python when
            # React calls it, so it's a shallow conversion (depth=1). We could have done
            # no pydict -> jsobject conversion (and send a PyProxy) but React expect an
            # object
            # pyodide.ffi.to_js(props, depth=1, dict_converter=js.Object.fromEntries),
            js.Object.fromEntries(props_as_list),
            # Same, this proxy will be converted back to the original Python object
            # when React calls the function, but this time we don't need to convert it
            # to a JS Array (React will pass it as is)
            # pyodide.ffi.create_proxy(children),
            *(cached_deep_to_js(child) for child in children),
        )

    return create


@weak_cached
def cached_wrap_prop(prop):
    if isinstance(prop, FunctionType):

        @functools.wraps(prop)
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
            wrapped = auto_start_async(wrapped)

        return pyodide.ffi.to_js(wrapped)
    elif is_proxy(prop):
        js_prop = pyodide.ffi.to_js(
            prop, depth=-1, dict_converter=js.Object.fromEntries
        )
        js.console.log(js_prop)
        return js_prop
    #     return pyodide.ffi.create_proxy(prop)
    else:
        return pyodide.ffi.to_js(prop, depth=-1, dict_converter=js.Object.fromEntries)


def stub_component(name, props_mapping) -> Callable[[T], T]:
    def make(fn):
        def create_fn(*children, **props):
            props_as_list = [
                [
                    props_mapping.get(k, k),
                    cached_wrap_prop(v),
                ]
                for k, v in props.items()
            ]
            return js.React.createElement(
                # element_type is either a str or a PyProxy of a JS function
                # such as window.JoyUI.Button
                name,
                # Deep convert to JS objects (depth=-1) to avoid issues with React
                js.Object.fromEntries(props_as_list),
                # Deep convert too, same reason
                *(cached_deep_to_js(child) for child in children),
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


def component(fn: Callable):
    """
    Decorator to turn a Python function into a Pret component, that
    will be rendered by React.

    Parameters
    ----------
    fn: Callable
    """
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

    def __reduce__(self):
        return self.dillable, ()

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


CallableParams = ParamSpec("ServerCallableParams")
CallableReturn = TypeVar("CallableReturn")


def server_only(
    fn: Callable[CallableParams, CallableReturn],
) -> Callable[CallableParams, Union[Awaitable[CallableReturn], CallableReturn]]:
    return pickle_as(fn, make_remote_callable(get_manager().register_function(fn)))
