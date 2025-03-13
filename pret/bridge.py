import asyncio
import functools
import sys
import weakref
from types import ModuleType
from weakref import WeakKeyDictionary

from pret.serialize import GlobalRef, pickle_as

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
            value = fn(obj)
        except KeyError:
            value = fn(obj)
            try:
                weak_cache[obj] = value
            except TypeError:
                pass
        return value

    return wrapped


@weak_cached
def cached_create_proxy(obj):
    if isinstance(obj, (int, float, str, bool)) or obj is None:
        return obj
    return pyodide.ffi.create_proxy(obj)


@weak_cached
def cached_deep_to_js(obj):
    return pyodide.ffi.to_js(obj, depth=-1)


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
