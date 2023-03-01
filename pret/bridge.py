import asyncio
import functools
import sys
from types import ModuleType
from weakref import WeakKeyDictionary

from pret.serialize import GlobalRef, pickle_as

js = ModuleType("js", None)
pyodide = ModuleType("pyodide", None)
sys.modules["js"] = js
sys.modules["pyodide"] = pyodide


def make_create_proxy():
    weak_references = WeakKeyDictionary()


    def create_proxy_client(x, _=None):
        try:
            res = weak_references[x]
        except KeyError:
            res = pyodide.ffi.create_proxy(x)
            weak_references[x] = res
        except TypeError:
            return pyodide.ffi.create_proxy(x)

        return res

    @pickle_as(create_proxy_client)
    class UnpickleAsProxy:
        def __init__(self, obj, proxy_list=None):
            assert proxy_list is None, "proxy_list is only supported in the browser, " \
                                       "i.e. inside your components"
            self.obj = obj

        def __reduce__(self):
            return create_proxy_client, (self.obj,)


    return UnpickleAsProxy


def make_to_js():
    weak_references = WeakKeyDictionary()

    def to_js(x, wrap=False, **kwargs):
        key = x
        if wrap:
            x = {"wrapped": x}
            kwargs['depth'] = 1
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



def make_stub_js_module(module_name, package_name, package_version):
    if sys.platform == 'emscripten':
        # running in Pyodide or other Emscripten based build
        return
    # Makes a dummy module with __name__ set to the module name
    # so that it can be pickled and unpickled
    full_module_name = "js." + module_name

    def make_stub_js_function(name):
        # Makes a dummy function with __module__ set to the module name
        # so that it can be pickled and unpickled
        ref = GlobalRef(module, name)
        setattr(module, name, ref)
        return ref

    module = ModuleType(full_module_name, f"Fake server side js module for {module_name}")
    module.__file__ = f"<{full_module_name}>"
    module.__getattr__ = make_stub_js_function
    module._package_name = package_name
    module._package_version = package_version
    sys.modules[module.__name__] = module
    setattr(js, module_name, module)


create_proxy = make_create_proxy()

to_js = make_to_js()
