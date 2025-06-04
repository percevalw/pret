"""
This module provides marshaling to convert Python objects (and functions, and classes)
into a format that can be serialized and later reconstructed in a JavaScript
environment.
"""

import ast
import base64
import collections
import contextlib
import functools
import inspect
import os
import pathlib
import re
import sys
import tempfile
import textwrap
import time
import types
import uuid
import weakref
from asyncio import Future
from io import BytesIO, StringIO
from pathlib import Path
from types import FunctionType
from typing import Any, Callable, Dict, Union
from weakref import WeakKeyDictionary

import astunparse
from transcrypt.__main__ import main as transcrypt

try:
    import cbor2._encoder as cbor2_encoder
except ImportError:
    # Python 3.7 and earlier do not have cbor2._encoder
    import cbor2.encoder as cbor2_encoder

ModuleType = type(sys)
BuiltinFunctionType = type(time.time)
marshal_overrides = WeakKeyDictionary()


def marshal_as(
    base_version: Any = None,
    marshaled_version: Any = None,
    js: str = None,
    globals: Union[dict, Callable[[], Dict]] = {},
):
    @functools.wraps(marshal_as)
    def wrap(fn):
        assert fn is not None
        if marshaled_version is not None:
            assert not isinstance(marshaled_version, str)
            marshalable = ("py", marshaled_version)
        else:
            assert js is not None
            if callable(globals):
                scoped_vars = globals()
            else:
                scoped_vars = globals
            marshalable = ("js", js, scoped_vars)
        marshal_overrides[fn] = marshalable
        return fn

    if base_version is None:
        return wrap
    if marshaled_version is None and js is None:
        marshaled_version = base_version
        return wrap

    return wrap(base_version)


class GlobalRef:
    def __init__(self, module, name):
        self.module = module
        self.name = name
        self.__module__ = module.__name__

    def __reduce__(self):
        # str is interpreted by pickle as save global, which is exactly what we want
        shared_marshaler().accessed_global_refs.add(self)
        return self.__module__ + "." + self.name

    def __hash__(self):
        return hash(self.__module__ + "." + self.name)

    def __repr__(self):
        return f"GlobalRef({self.__module__}.{self.name})"

    def __str__(self):
        return f"{self.__module__}.{self.name}"


js = ModuleType("js", None)
marshal_as(js, js="return window.pret_modules.js")


def make_stub_js_module(
    global_name,
    py_package_name,
    js_package_name,
    package_version,
    stub_qualified_name,
):
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
    marshal_as(module, js=f"return pret_modules.js.{global_name};")
    setattr(js, global_name, module)


@contextlib.contextmanager
def redirect_argv(*args):
    sys._argv = sys.argv[:]
    sys.argv = list(args)
    yield
    sys.argv = sys._argv


@contextlib.contextmanager
def capture_stdout():
    new_target = StringIO()
    old_target = sys.stdout
    sys.stdout = new_target
    try:
        yield new_target
    finally:
        sys.stdout = old_target


marshal_as(
    Future,
    js="""
import {Exception} from './org.transcrypt.__runtime__.js';

var CancelledError = _class_ ('CancelledError', [Exception], {
    __module__: __name__,
    get __init__() {return __get__(this, function(self, message) {
        Error.call(self, message || 'Future was cancelled');
        self.name = 'CancelledError';
        self.message = message || 'Future was cancelled';
    });}
});

var Future = _class_ ('Future', [object], {
    __module__: __name__,
    // States
    PENDING: 0,
    FINISHED: 1,
    CANCELLED: 2,

    get __init__() {return __get__(this, function(self) {
        self._state = self.PENDING;
        self._result = undefined;
        self._exception = undefined;
        self._promise = new Promise(function(resolve, reject) {
            self._resolve = resolve;
            self._reject = reject;
        });
    });},

    // helpers for awaiting
    get then() {return __get__(this, function(self, onFulfilled, onRejected) {
        return self._promise.then(onFulfilled, onRejected);
    });},
    get catch() {return __get__(this, function(self, onRejected) {
        return self._promise.catch(onRejected);
    });},
    get finally() {return __get__(this, function(self, onFinally) {
        return self._promise.finally(onFinally);
    });},

    // properties
    get done() {return __get__(this, function(self) {
        return self._state !== self.PENDING;
    });},
    get cancelled() {return __get__(this, function(self) {
        return self._state === self.CANCELLED;
    });},
    get result() {return __get__(this, function(self) {
        if (!self.done()) throw new Error('Future not done yet');
        if (self.cancelled()) throw new Error('Future was cancelled');
        if (self._exception !== undefined) throw self._exception;
        return self._result;
    });},
    get exception() {return __get__(this, function(self) {
        return self.done() ? self._exception : undefined;
    });},

    // mutators
    get set_result() {return __get__(this, function(self, value) {
        if (self.done()) return false;
        self._state = self.FINISHED;
        self._result = value;
        self._resolve(value);
        return true;
    });},
    get set_exception() {return __get__(this, function(self, err) {
        if (self.done()) return false;
        self._state = self.FINISHED;
        self._exception = err instanceof Error ? err : new Error(String(err));
        self._reject(self._exception);
        return true;
    });},
    get cancel() {return __get__(this, function(self, msg) {
        if (self.done()) return false;
        self._state = self.CANCELLED;
        self._exception = CancelledError(msg || 'Future was cancelled');
        self._reject(self._exception);
        return true;
    });}
});

Future.CancelledError = CancelledError;

return Future;
""",
)


PRE_TRANSPILED_MODULES = {
    "builtins": "org.transcrypt.__runtime__",
    "cmath": "cmath",
    "copy": "copy",
    "dataclasses": "dataclasses",
    "datetime": "datetime",
    "functools": "functools",
    "itertools": "itertools",
    "json": "json",
    "logging": "logging",
    "math": "math",
    "random": "random",
    "re": "re",
    "time": "time",
    "typing": "typing",
    "unicodedata": "unicodedata",
    "warnings": "warnings",
    "weakref": "weakref",
}


def inspect_scopes(f):
    global_vars = set()
    stack = [f.__code__]
    while stack:
        code = stack.pop()
        stack.extend(k for k in code.co_consts if isinstance(k, types.CodeType))
        global_vars |= {n for n in code.co_names}

    globals_vals = {
        n: f.__globals__[n] for n in sorted(global_vars) if n in f.__globals__
    }
    closure_vals = (
        {}
        if f.__closure__ is None
        else {
            n: cell.cell_contents
            for n, cell in zip(f.__code__.co_freevars, f.__closure__)
        }
    )
    return {**globals_vals, **closure_vals}


class StrictCBOREncoder(cbor2_encoder.CBOREncoder):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for base, marshaled in marshal_overrides.items():
            if marshaled[0] == "py" and marshaled[1] in self._encoders:
                # If we have a py marshalable, we can use it directly
                self._encoders[base] = self._encoders[marshaled[1]]

    def _find_encoder(
        self, obj_type: type
    ) -> "Callable[[cbor2_encoder.CBOREncoder, Any], None] | None":
        for type_or_tuple, enc in list(self._encoders.items()):
            if type(type_or_tuple) is tuple:
                try:
                    modname, typename = type_or_tuple
                except (TypeError, ValueError):
                    raise cbor2_encoder.CBOREncodeValueError(
                        f"invalid deferred encoder type {type_or_tuple!r} (must be a "
                        "2-tuple of module name and type name, e.g. "
                        "('collections', 'defaultdict'))"
                    )

                imported_type = getattr(sys.modules.get(modname), typename, None)
                if imported_type is not None:
                    del self._encoders[type_or_tuple]
                    self._encoders[imported_type] = enc
                    type_ = imported_type
                else:  # pragma: nocover
                    continue
            else:
                type_ = type_or_tuple

            if obj_type is type_:
                self._encoders[obj_type] = enc
                return enc
        return None

    def encode(self, obj: Any) -> None:
        obj_type = obj.__class__
        if obj_type in self._encoders:
            encoder = self._encoders[obj_type] or self._default
        else:
            encoder = self._find_encoder(obj_type) or self._default
        if not encoder:
            raise cbor2_encoder.CBOREncodeTypeError(
                f"cannot serialize type {obj_type.__name__}"
            )

        encoder(self, obj)


class Pretmarshaler:
    def __init__(self):
        self.source_codes = {}
        self.accessed_global_refs = set()
        self.encoder = cbor2_encoder.shareable_encoder(self._encoder)
        self.id = uuid.uuid4().hex
        self.chunk_idx = 0
        self._file = BytesIO()
        self._cbor_encoder = StrictCBOREncoder(
            self._file, default=self.encoder, value_sharing=True
        )

    def transpile(self):
        py_module_string = (
            "__pragma__ ('kwargs')\n"
            + "\n".join(v[1] for k, v in self.source_codes.items() if v[0] == "py")
            + "\n__pragma__ ('nokwargs')\n"
        )
        tmpdir = Path(tempfile.mkdtemp())
        os.makedirs(tmpdir / "__target__", exist_ok=True)
        with open(tmpdir / "__main__.py", "w") as f:
            f.write(py_module_string)
        with redirect_argv(
            *("-b", "-n", "-m", "-p", "window"),
            str(tmpdir / "__main__.py"),
        ):
            with capture_stdout() as output:
                transcrypt()
            if "Saving" not in output.getvalue() or "Error" in output.getvalue():
                raise Exception(
                    "Could not export your Python app to Javascript with Transcrypt. "
                    "Here is the log: \n{}".format(output.getvalue())
                )
        res = (tmpdir / "__target__/__main__.js").read_text()
        res += "\n" + "\n".join(
            v[1] for k, v in self.source_codes.items() if v[0] == "js"
        )
        for k, v in self.source_codes.items():
            assert v[1].strip() != "", f"Source code for {k} is empty: {v}"
        (tmpdir / "__target__/__main__.js").write_text(res)
        return tmpdir

    def process_js_module(self, js_module):
        has_default = False
        if "export default " in js_module:
            js_module = js_module.replace("\nexport default ", "\nvar _default_ = ")
            has_default = True
        exported = [
            match.group(1) or match.group(2).strip()
            for match in re.finditer(
                r"\nexport\s+(?:(?:var|function|let|const)?\s+([\w_]+)|{([^}]*)})",
                js_module,
            )
        ]
        imports = []
        js_module = js_module.replace("\nexport ", "")

        if has_default:
            exported.append("_default_")
        js_module += "\nreturn {" + ", ".join(exported) + "};"

        def replace_js_import(match):
            imported = match.group(1).strip()
            mod_key = match.group(2).strip()
            if mod_key.endswith(".js"):
                mod_key = mod_key[:-3]
            if mod_key.startswith("./"):
                mod_key = mod_key[2:]
            mod_key = mod_key.replace("/", ".")

            imports.append(mod_key)

            non_bracket_part, named_part = (
                (None, imported)
                if imported.startswith("{")
                else (
                    imported.split(",", 1)[0].strip(),
                    imported.split(",", 1)[1].strip() if "," in imported else None,
                )
            )

            lines = []
            if non_bracket_part:
                if non_bracket_part.startswith("* as "):
                    non_bracket_part = non_bracket_part[4:].strip()
                    lines.append(f"var {non_bracket_part} = pret_modules['{mod_key}'];")
                else:
                    lines.append(
                        f"var {non_bracket_part} = pret_modules['{mod_key}']._default_;"
                    )

            if named_part:
                inner = named_part[1:-1].strip()
                specs = []
                for s in (s.strip() for s in inner.split(",") if s.strip()):
                    specs.append(
                        f"{s.split(' as ')[0].strip()}: {s.split(' as ')[1].strip()}"
                        if " as " in s
                        else s
                    )
                lines.append(
                    f"var {{ {', '.join(specs)} }} = pret_modules['{mod_key}'];"
                )

            return "\n".join(lines)

        js_module = re.sub(
            "^import\\s+([^;]+)\\s+from\\s+['\"]([^'\"]+)['\"];",
            replace_js_import,
            js_module,
            flags=re.MULTILINE,
        )
        return js_module, imports

    def dump(self, obj):
        chunk_idx = self.chunk_idx
        self._cbor_encoder.encode(obj)
        blob = self._file.getvalue()
        self.chunk_idx += 1
        header = (
            "if(window.pret_modules===undefined){window.pret_modules={};}\n"
            "var pret_modules=window.pret_modules;\n"
            "pret_modules.js=window;\n"
        )
        tmpdir = self.transpile()
        mods, deps = {}, {}
        for p in pathlib.Path(tmpdir / "__target__").glob("**/*.js"):
            code, imps = self.process_js_module(p.read_text())
            module_name = p.stem
            if module_name == "__main__":
                mods[module_name] = code
            else:
                mods[module_name] = (
                    f"pret_modules['{module_name}']=(function(){{{code}}})();"
                )
            deps[module_name] = {pathlib.Path(i).name for i in imps}

        indeg = {m: sum(d in mods for d in deps[m]) for m in mods}
        queue = collections.deque([m for m, n in indeg.items() if n == 0])
        order = []
        while queue:
            m = queue.popleft()
            order.append(m)
            for n in mods:
                if m in deps[n]:
                    indeg[n] -= 1
                    if indeg[n] == 0:
                        queue.append(n)
        order += [m for m in mods if m not in order]  # fallback for cycles
        order.remove("org.transcrypt.__runtime__")  # already bundled in js

        js = (
            header
            + "\n\n".join(mods[m] for m in order)
            + "\n//# sourceURL=dynamic_factory.js"
        )
        return [base64.encodebytes(blob).decode(), js], chunk_idx

    def _encoder(self, encoder, value):
        source_code_id = f"pret_factory_{len(self.source_codes)}"
        try:
            if value in marshal_overrides:
                marshalable = marshal_overrides[value]
                marshal_mode = marshalable[0]
                target = marshalable[1]
                if marshal_mode == "py":
                    # Python marshalable, we can just use the value
                    self.encoder(encoder, target)
                    return
                elif marshal_mode == "js":
                    # JS marshalable, we need to encode it as a tag
                    globals = marshalable[2] if len(marshalable) > 2 else {}
                    args = ", ".join(globals.keys())
                    factory_code = (
                        f"function {source_code_id}({args})"
                        " {\n" + f"{target.strip()};\n" + "}\n"
                        f"export {{{source_code_id}}};\n"
                    )
                    self.source_codes[source_code_id] = ("js", factory_code)
                    encoder.encode(
                        cbor2_encoder.CBORTag(
                            4000, [source_code_id, list(globals.values())]
                        )
                    )
                    return
            elif getattr(value, "__module__", None) in PRE_TRANSPILED_MODULES:
                # If the function is from a pre-transpiled module, we can just use its
                # name.
                factory_code = (
                    f"def {source_code_id}():\n"
                    f"  from {PRE_TRANSPILED_MODULES[value.__module__]} import {value.__name__}\n"  # noqa: E501
                    f"  return {value.__name__}\n"
                )
                self.source_codes[source_code_id] = ("py", factory_code)
                encoder.encode(cbor2_encoder.CBORTag(4000, [source_code_id, []]))
                return
            elif (
                isinstance(value, ModuleType)
                and value.__name__ in PRE_TRANSPILED_MODULES
            ):
                factory_code = (
                    f"def {source_code_id}():\n"
                    f"  import {PRE_TRANSPILED_MODULES[value.__name__]} as module\n"
                    f"  return module\n"
                )
                self.source_codes[source_code_id] = ("py", factory_code)
                encoder.encode(cbor2_encoder.CBORTag(4000, [source_code_id, []]))
                return
            elif isinstance(value, (FunctionType, BuiltinFunctionType)):
                # code = pygetsource.getfactory(value.__code__, function_name="_fn_")
                code = inspect.getsource(value.__code__)
                code = textwrap.dedent(code)
                scoped_vars = inspect_scopes(value)
                if "__class__" in scoped_vars:
                    # do assert here ?
                    scoped_vars.pop("__class__")
                # Factory will be used to create the fn or cls
                tree = ast.parse(code)
                tree.body[0].decorator_list = []
                factory = ast.FunctionDef(
                    name=source_code_id,
                    args=ast.arguments(
                        args=[ast.arg(arg=n, annotation=None) for n in scoped_vars],
                        kwarg=None,
                        vararg=None,
                        defaults=[],
                    ),
                    body=[
                        tree,
                        ast.Return(value=ast.Name(id=value.__name__, ctx=ast.Load())),
                    ],
                    decorator_list=[],
                )
                ast.fix_missing_locations(factory)
                factory_code = astunparse.unparse(factory)
                self.source_codes[source_code_id] = ("py", factory_code)
                encoder.encode(
                    cbor2_encoder.CBORTag(
                        4000, [source_code_id, list(scoped_vars.values())]
                    )
                )
                return
            elif isinstance(value, type):
                # 1. Get own members of the class (members that are not inherited)
                base = value.__base__
                members_names = sorted(
                    set(dir(value))
                    - {
                        "__dict__",
                        "__weakref__",
                        "__slots__",
                        "__subclasshook__",
                        "__init_subclass__",
                        "__abstractmethods__",
                    }
                )
                base_members_names = sorted(
                    set(dir(base))
                    - {
                        "__dict__",
                        "__weakref__",
                        "__slots__",
                        "__subclasshook__",
                        "__init_subclass__",
                        "__abstractmethods__",
                    }
                )
                members = [(name, getattr(value, name)) for name in members_names]
                parent_members = (
                    [(n, getattr(base, n)) for n in base_members_names] if base else []
                )
                own_members = [
                    member
                    for member in members
                    if member not in parent_members and member[0]
                ]
                methods = [
                    member
                    for member in own_members
                    if isinstance(member[1], FunctionType)
                ]
                encoder.encode(
                    cbor2_encoder.CBORTag(
                        4001,
                        [
                            value.__name__,
                            value.__bases__,
                            {n: m for n, m in own_members if n not in methods},
                            {n: m for n, m in methods if n not in own_members},
                        ],
                    )
                )
                return
            elif inspect.isclass(type(value)):
                if isinstance(value, ModuleType):
                    raise ValueError(value)
                if (
                    hasattr(value, "__reduce__")
                    and type(value).__reduce__ is not object.__reduce__
                ):
                    # If the object has a __reduce__ method,
                    # we can use it to get the state
                    reduced = value.__reduce__()
                    if isinstance(reduced, str):
                        factory_code = (
                            f"function {source_code_id}() {{\n"
                            f"  return pret_modules.{reduced};\n"
                            "}\n"
                            f"export {{{source_code_id}}};\n"
                        )
                        self.source_codes[source_code_id] = ("js", factory_code)
                        encoder.encode(
                            cbor2_encoder.CBORTag(4000, [source_code_id, []])
                        )
                        return
                    else:
                        encoder.encode(cbor2_encoder.CBORTag(4002, reduced))
                        return
                else:
                    try:
                        state = value.__dict__
                    except AttributeError:
                        state = {}
                    encoder.encode(
                        cbor2_encoder.CBORTag(
                            4003, [type(value), state, type(value).__name__]
                        )
                    )
                    return
            raise ValueError()
        except (RecursionError, TypeError):
            import traceback

            traceback.print_exc()
            raise ValueError(
                f"Could not marshal {value} of type {type(value)}. "
                "Please ensure it is a marshalable type."
            )
        except:
            raise


shared_marshaler: Any = None


def get_shared_marshaler():
    global shared_marshaler
    if shared_marshaler is None or shared_marshaler() is None:
        marshaler = Pretmarshaler()
        shared_marshaler = weakref.ref(marshaler)
    return shared_marshaler()


def clear_shared_marshaler():
    global shared_marshaler
    shared_marshaler = None
