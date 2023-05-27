import base64
import functools
import inspect
from typing import Callable, TypeVar

from pret.bridge import auto_start_async, create_proxy, js, pyodide
from pret.manager import get_manager
from pret.serialize import PretPickler, get_shared_pickler, pickle_as

T = TypeVar("T")


def create_element(element_type, props, *children):
    result = js.React.createElement(
        element_type,
        props,
        *pyodide.ffi.to_js(children),
    )
    return result


def make_create_element_from_function(fn, with_proxy=False):
    if isinstance(fn, str):
        react_type_fn = fn
    else:

        @create_proxy
        def react_type_fn(props, ctx=None):
            props = props.to_py(depth=1)
            children = props.pop("children", ())
            return fn(*children, **props)

    def create(*children, **props):
        return create_element(
            react_type_fn if isinstance(react_type_fn, str) else react_type_fn,
            js.Object.fromEntries(pyodide.ffi.to_js(props)),
            children,
        )

    return create


def stub_component(name, props_mapping) -> Callable[[T], T]:
    def make(fn):
        def wrapped(*children, **props):
            js_props = pyodide.ffi.to_js(
                {
                    props_mapping.get(k, k): auto_start_async(v)
                    # check if callable
                    if inspect.iscoroutinefunction(v) else v
                    for k, v in props.items()
                },
                dict_converter=js.Object.fromEntries,
            )
            return create_element(
                name,
                js_props,
                *children,
            )

        return wrapped

    return make


def component(fn):
    create_fn = make_create_element_from_function(fn)

    @functools.wraps(fn)
    @pickle_as(create_fn)
    def wrapped(*children, detach=False, **props):
        return Renderable(
            lambda: create_fn(*children, **props),
            detach=detach,
        )

    return wrapped


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
