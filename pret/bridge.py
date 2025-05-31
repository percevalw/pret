import asyncio
import functools
import gc
import inspect
import sys
import traceback
import weakref
from types import FunctionType, ModuleType
from weakref import WeakKeyDictionary

from pret.serialize import GlobalRef, pickle_as

cache = {}
js = ModuleType("js", None)
pyodide = ModuleType("pyodide", None)
sys.modules["js"] = js
sys.modules["pyodide"] = pyodide


weak_cache = weakref.WeakKeyDictionary()


def weak_cached(fn):
    # run pyodide.ffi.to_js(obj, **kwargs) or use a weak cache
    # if obj is weakrefable
    def wrapped(obj):
        try:
            value = weak_cache[obj]
        except TypeError:
            value = cached_apply(obj, fn)
        except KeyError:
            value = fn(obj)
            try:
                weak_cache[obj] = value
            except TypeError:
                pass
        return value

    return wrapped


def cached_apply(x, fn):
    idx = id(x)
    try:
        return cache[idx][1]
    except KeyError:
        pass
    y = fn(x)
    cache[idx] = (x, y)
    return y


def cached(fn):
    def wrap(x):
        return cached_apply(x, fn)

    return wrap


def wrap_function(prop):
    def wrapped(*args, **kwargs):
        args = [
            arg.to_py() if isinstance(arg, pyodide.ffi.JsProxy) else arg for arg in args
        ]
        kwargs = {
            kw: arg.to_py() if isinstance(arg, pyodide.ffi.JsProxy) else arg
            for kw, arg in kwargs
        }
        return prop(*args, **kwargs)

    if inspect.iscoroutinefunction(prop):
        wrapped = auto_start_async(wrapped)

    return pyodide.ffi.create_proxy(wrapped)


def prop_eager_converter(obj, convert, cache):
    if isinstance(obj, FunctionType):
        return cached_apply(obj, wrap_function)
    else:
        return cached_apply(obj, convert)


@weak_cached
def cached_prop_to_js(obj):
    if isinstance(obj, FunctionType):
        return wrap_function(obj)
    try:
        return pyodide.ffi.to_js(
            obj,
            depth=-1,
            dict_converter=js.Object.fromEntries,
            # eager_converter=prop_eager_converter,
        )
    except:
        traceback.print_exc()
        raise


def simple_eager_converter(obj, convert, cache):
    return cached_apply(obj, convert)


@weak_cached
def cached_to_js(obj):
    return pyodide.ffi.to_js(
        obj,
        depth=-1,
        dict_converter=js.Object.fromEntries,
        eager_converter=simple_eager_converter,
    )


def cached_create_proxy(obj):
    return cached_apply(obj, pyodide.ffi.create_proxy)


def gc_pret_callback(phase, info):
    if phase != "start":
        return

    ids_to_delete = []
    for entry in cache.items():
        # refcount <= 2 because:
        # - one is for the tuple (x, y) entry's value
        # - one is for temp passed parameter to sys.getrefcount
        refcount = sys.getrefcount(entry[1][0])
        # print("Ref count for", entry[0], "is", refcount, flush=True)
        if refcount <= 2:
            # print("Deleting", entry[0], flush=True)

            ids_to_delete.append(entry[0])
    for idx in ids_to_delete:
        del cache[idx]


# just in case we rerun this
gc.callbacks[:] = [fn for fn in gc.callbacks if fn.__name__ != "gc_pret_callback"]
gc.callbacks.append(gc_pret_callback)


def make_create_proxy():
    weak_references = WeakKeyDictionary()

    def create_proxy_client(x, wrap_in_browser=None, _=None):
        try:
            res = weak_references[x]
        except KeyError:
            res = pyodide.ffi.create_proxy(x)
            if wrap_in_browser is not None:
                res = wrap_in_browser(res)
            weak_references[x] = res
        except TypeError:
            res = pyodide.ffi.create_proxy(x)
            if wrap_in_browser is not None:
                res = wrap_in_browser(res)

        return res

    @pickle_as(create_proxy_client)
    class UnpickleAsProxy:
        def __init__(self, obj, wrap_in_browser=None, proxy_list=None):
            assert proxy_list is None, (
                "proxy_list is only supported in the browser, "
                "i.e. inside your components"
            )
            self.obj = obj
            self.wrap_in_browser = wrap_in_browser

        def __reduce__(self):
            return create_proxy_client, (self.obj, self.wrap_in_browser)

    return UnpickleAsProxy


def make_to_js():
    weak_references = WeakKeyDictionary()

    def to_js(x, wrap=False, **kwargs):
        key = x
        if wrap:
            x = {"wrapped": x}
            kwargs["depth"] = 1
        try:
            res = weak_references[key]
        except KeyError:
            res = pyodide.ffi.to_js(x, **kwargs)
            weak_references[key] = res
        except TypeError:
            return pyodide.ffi.to_js(x, **kwargs)

        return res

    return to_js


def auto_start_async(coro_func):
    if coro_func is None:
        return

    @functools.wraps(coro_func)
    def wrapper(*args, **kwargs):
        loop = asyncio.get_event_loop()
        task = loop.create_task(coro_func(*args, **kwargs))
        return task

    return wrapper


def make_stub_js_module(
    global_name,
    py_package_name,
    js_package_name,
    package_version,
    stub_qualified_name,
):
    if sys.platform == "emscripten":
        # running in Pyodide or other Emscripten based build
        return
    # Makes a dummy module with __name__ set to the module name
    # so that it can be pickled and unpickled
    full_global_name = "js." + global_name

    def make_stub_js_function(name):
        # Makes a dummy function with __module__ set to the module name
        # so that it can be pickled and unpickled
        ref = GlobalRef(module, name)
        setattr(module, name, ref)
        return ref

    module = ModuleType(
        full_global_name, f"Fake server side js module for {global_name}"
    )
    module.__file__ = f"<{full_global_name}>"
    module.__getattr__ = make_stub_js_function
    module._js_package_name = js_package_name
    module._package_name = py_package_name
    module._package_version = package_version
    module._stub_qualified_name = stub_qualified_name
    sys.modules[module.__name__] = module
    setattr(js, global_name, module)


create_proxy = make_create_proxy()

to_js = make_to_js()
