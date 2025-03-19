import fnmatch
import importlib
import io
import sys
import uuid
import warnings
import weakref
from inspect import iscode, isfunction, ismethod
from pickle import DICT, EMPTY_DICT, MARK
from typing import Any
from weakref import WeakKeyDictionary, WeakSet, WeakValueDictionary

from dill._dill import (
    CodeType,
    EllipsisType,
    FunctionType,
    ModuleType,
    NotImplementedType,
    Pickler,
    PicklingWarning,
    StockPickler,  # noqa: F401
    TypeType,
    __builtin__,
    _create_function,
    _create_namedtuple,
    _create_type,
    _getattribute,
    _import_module,
    _is_builtin_module,
    _is_imported_module,
    _load_type,
    _repr_dict,
    _save_with_postproc,
    _setitems,
    _typemap,
    is_dill,
    logger,
    save_code,
)

try:
    from dill._dill import singletontypes
except ImportError:
    from dill._dill import IPYTHON_SINGLETONS as singletontypes

from dill.detect import getmodule, nestedglobals
from pygetsource import getfactory

from pret.settings import settings


def save_code_as_source(pickler, obj):
    if not (
        pickler.save_code_as_source is True
        or pickler.save_code_as_source == "auto"
        and sys.version_info < (3, 11)
    ):
        save_code(pickler, obj)
        return

    factory_code = getfactory(obj)

    pickler.save_reduce(create_code_from_source, (factory_code,), obj=obj)


def create_code_from_source(source_code):
    var_dict = {}
    exec(source_code, var_dict, var_dict)
    return var_dict["_fn_"].__code__


class GlobalRef:
    def __init__(self, module, name):
        self.module = module
        self.name = name
        self.__module__ = module.__name__

    def __reduce__(self):
        # str is interpreted by pickle as save global, which is exactly what we want
        shared_pickler().accessed_global_refs.add(self)
        return self.name

    def __hash__(self):
        return hash(self.__module__ + "." + self.name)

    def __repr__(self):
        return f"GlobalRef({self.__module__}.{self.name})"

    def __str__(self):
        return f"{self.__module__}.{self.name}"


def pickle_as(base_version: Any, pickled_version: Any = None):
    def wrap(fn):
        fn._dillable = pickled_version
        return fn

    if pickled_version is None:
        pickled_version = base_version
        return wrap

    return wrap(base_version)


do_not_recurse_in_functions_map = WeakSet()


def do_not_recurse_in_function(func):
    do_not_recurse_in_functions_map.add(func)
    return func


def globalvars(func, recurse=True, builtin=False):
    """
    Adapted from dill._dill._globalvars

    get objects defined in global scope that are referred to by func

    return a dict of {name:object}"""
    if ismethod(func):
        func = func.__func__
    if isfunction(func):
        if "_dillable" in func.__dict__:
            func = func._dillable
        globs = vars(getmodule(sum)).copy() if builtin else {}
        # get references from within closure
        orig_func, func = func, set()
        for obj in orig_func.__closure__ or {}:
            try:
                cell_contents = obj.cell_contents
            except ValueError:  # cell is empty
                pass
            else:
                _vars = globalvars(cell_contents, recurse, builtin) or {}
                func.update(_vars)  # XXX: (above) be wary of infinte recursion?
                globs.update(_vars)
        # get globals
        globs.update(orig_func.__globals__ or {})
        # get names of references
        if not recurse:
            func.update(orig_func.__code__.co_names)
        else:
            func.update(nestedglobals(orig_func.__code__))
            # find globals for all entries of func
            for key in func.copy():  # XXX: unnecessary...?
                nested_func = globs.get(key)
                if nested_func is orig_func:
                    # func.remove(key) if key in func else None
                    continue  # XXX: globalvars(func, False)?
                func.update(globalvars(nested_func, True, builtin))
    elif iscode(func):
        globs = vars(getmodule(sum)).copy() if builtin else {}
        # globs.update(globals())
        if not recurse:
            func = func.co_names  # get names
        else:
            orig_func = func.co_name  # to stop infinite recursion
            func = set(nestedglobals(func))
            # find globals for all entries of func
            for key in func.copy():  # XXX: unnecessary...?
                if key is orig_func:
                    # func.remove(key) if key in func else None
                    continue  # XXX: globalvars(func, False)?
                nested_func = globs.get(key)
                func.update(globalvars(nested_func, True, builtin))
    else:
        return {}
    # NOTE: if name not in __globals__, then we skip it...
    return dict((name, globs[name]) for name in func if name in globs)


def filter_patterns(patterns, query):
    return any(fnmatch.fnmatch(query, p) for p in patterns)


def _locate_function(obj, pickler=None):
    """Adapter for dill._dill._locate_function"""
    module_name = getattr(obj, "__module__", None)

    if (
        module_name is None
        or filter_patterns(["__main__", *pickler.pickled_modules], module_name)
        or pickler
        and is_dill(pickler, child=False)
        and pickler._session
        and module_name == pickler._main.__name__
    ):
        return False
    if hasattr(obj, "__qualname__"):
        module = _import_module(module_name, safe=True)
        try:
            found, _ = _getattribute(module, obj.__qualname__)
            return found is obj
        except AttributeError:
            return False
    else:
        found = _import_module(module_name + "." + obj.__name__, safe=True)
        return found is obj


def save_function(pickler, obj):
    """Adapted from dill._dill.save_function"""
    if not _locate_function(obj, pickler):
        if type(obj.__code__) is not CodeType:
            # Some PyPy builtin functions have no module name, and thus are not
            # able to be located
            module_name = getattr(obj, "__module__", None)
            if module_name is None:
                module_name = __builtin__.__name__
            module = _import_module(module_name, safe=True)
            _pypy_builtin = False
            try:
                found, _ = _getattribute(module, obj.__qualname__)
                if getattr(found, "__func__", None) is obj:
                    _pypy_builtin = True
            except AttributeError:
                pass

            if _pypy_builtin:
                logger.trace(pickler, "F3: %s", obj)
                pickler.save_reduce(getattr, (found, "__func__"), obj=obj)
                logger.trace(pickler, "# F3")
                return

        logger.trace(pickler, "F1: %s", obj)
        _recurse = getattr(pickler, "_recurse", None)

        _postproc = getattr(pickler, "_postproc", None)
        _main_modified = getattr(pickler, "_main_modified", None)
        _original_main = getattr(pickler, "_original_main", __builtin__)  # 'None'
        postproc_list = []

        if _recurse and obj not in do_not_recurse_in_functions_map:
            # recurse to get all globals referred to by obj
            globs_copy = globalvars(obj, recurse=True, builtin=True)

            # Add the name of the module to the globs dictionary to prevent
            # the duplication of the dictionary. Pickle the unpopulated
            # globals dictionary and set the remaining items after the function
            # is created to correctly handle recursion.
            if obj.__module__ not in pickler.modules_dict:
                pickler.modules_dict[obj.__module__] = {"__name__": obj.__module__}

            globs = pickler.modules_dict[obj.__module__]
        else:
            globs_copy = obj.__globals__

            # If the globals is the __dict__ from the module being saved as a
            # session, substitute it by the dictionary being actually saved.
            if _main_modified and globs_copy is _original_main.__dict__:
                globs_copy = getattr(pickler, "_main", _original_main).__dict__
                globs = globs_copy
            # If the globals is a module __dict__, do not save it in the pickle.
            elif (
                globs_copy is not None
                and obj.__module__ is not None
                and getattr(_import_module(obj.__module__, True), "__dict__", None)
                is globs_copy
            ):
                globs = globs_copy
            else:
                globs = {"__name__": obj.__module__}

        if globs_copy is not None and globs is not globs_copy:
            # In the case that the globals are copied, we need to ensure that
            # the globals dictionary is updated when all objects in the
            # dictionary are already created.
            glob_ids = {id(g) for g in globs_copy.values()}
            for stack_element in _postproc:
                if stack_element in glob_ids:
                    _postproc[stack_element].append((_setitems, (globs, globs_copy)))
                    break
            else:
                postproc_list.append((_setitems, (globs, globs_copy)))

        closure = obj.__closure__
        state_dict = {}
        for fattrname in ("__doc__", "__kwdefaults__", "__annotations__"):
            fattr = getattr(obj, fattrname, None)
            if fattr is not None:
                state_dict[fattrname] = fattr
        if obj.__qualname__ != obj.__name__:
            state_dict["__qualname__"] = obj.__qualname__
        if "__name__" not in globs or obj.__module__ != globs["__name__"]:
            state_dict["__module__"] = obj.__module__

        state = obj.__dict__
        if type(state) is not dict:
            state_dict["__dict__"] = state
            state = None
        if state_dict:
            state = state, state_dict

        _save_with_postproc(
            pickler,
            (
                _create_function,
                (obj.__code__, globs, obj.__name__, obj.__defaults__, closure),
                state,
            ),
            obj=obj,
            postproc_list=postproc_list,
        )

        # Lift closure cell update to earliest function (#458)
        if _postproc:
            topmost_postproc = next(iter(_postproc.values()), None)
            if closure and topmost_postproc:
                for cell in closure:
                    possible_postproc = (setattr, (cell, "cell_contents", obj))
                    try:
                        topmost_postproc.remove(possible_postproc)
                    except ValueError:
                        continue

                    # Change the value of the cell
                    pickler.save_reduce(*possible_postproc)
                    # pop None created by calling preprocessing step off stack
                    pickler.write(bytes("0", "UTF-8"))

        logger.trace(pickler, "# F1")
    else:
        logger.trace(pickler, "F2: %s", obj)
        name = getattr(obj, "__qualname__", getattr(obj, "__name__", None))
        StockPickler.save_global(pickler, obj, name=name)
        logger.trace(pickler, "# F2")
    return


def save_module_dict(pickler, obj):
    """Adapted from dill._dill._save_module_dict"""

    # If the dict being saved is the __dict__ of a module
    if (
        "__name__" in obj
        and type(obj["__name__"]) is str
        and obj is getattr(_import_module(obj["__name__"], True), "__dict__", None)
        and not filter_patterns(pickler.pickled_modules, obj["__name__"])
    ):
        logger.trace(pickler, "D4: %s", _repr_dict(obj))  # obj
        pickler.write(bytes("c%s\n__dict__\n" % obj["__name__"], "UTF-8"))
        logger.trace(pickler, "# D4")
    elif (
        "__name__" in obj
        and type(obj["__name__"]) is str
        and obj is pickler.modules_dict.get(obj["__name__"], None)
        and not obj["__name__"] == __name__
    ):
        if obj["__name__"] not in pickler.saving_modules:
            # we will recreate the module using the create_module function when loading
            pickler.saving_modules.add(obj["__name__"])
            pickler.save_reduce(
                create_module_with_dict,
                (
                    obj["__name__"],
                    obj,
                    obj.get("__doc__", None),
                    obj.get("__file__", None),
                ),
                obj=obj,
            )
        # pickler.write(POP)
        # pickler.write(bytes("c%s\n__dict__\n" % obj["__name__"], "UTF-8"))
        else:
            if pickler.bin:
                pickler.write(EMPTY_DICT)
            else:  # proto 0 -- can't use EMPTY_DICT
                pickler.write(MARK + DICT)
            # DISABLE MEMOIZATION FOR THE ORIGINAL DICT
            # We want the original dict to be mapped to the module, returned
            # (and therefore memoized) by the create_module_with_dict function
            # self.memoize(obj)

            pickler._batch_setitems(obj.items())
    else:
        logger.trace(pickler, "D2: %s", _repr_dict(obj))  # obj
        if is_dill(pickler, child=False) and pickler._session:
            # we only care about session the first pass thru
            pickler._first_pass = False
        StockPickler.save_dict(pickler, obj)
        logger.trace(pickler, "# D2")


def create_module_with_dict(name, module_dict, doc=None, file=None):
    """
    Create a module with the given name, docstring, and optional file name
    """
    path_parts = name.split(".")
    for i in range(1, len(path_parts)):
        pname = ".".join(path_parts[:i])
        try:
            importlib.import_module(pname)
        except (ModuleNotFoundError, ImportError):
            pmodule = sys.modules[pname] = ModuleType(pname, "")
            pmodule.__file__ = pname
            pmodule.__builtins__ = __builtins__

    try:
        module = importlib.import_module(name)
    except (ModuleNotFoundError, ImportError):
        module = sys.modules[name] = ModuleType(name, doc)
        module.__file__ = file if file else "<%s>" % name
        module.__builtins__ = __builtins__

    module.__dict__.update(module_dict)

    return module.__dict__


def save_type(pickler, obj, postproc_list=None):
    """Adapted from dill._dill._save_type"""
    if obj in _typemap:
        logger.trace(pickler, "T1: %s", obj)
        pickler.save_reduce(_load_type, (_typemap[obj],), obj=obj)
        logger.trace(pickler, "# T1")
    elif obj.__bases__ == (tuple,) and all(
        [hasattr(obj, attr) for attr in ("_fields", "_asdict", "_make", "_replace")]
    ):
        # special case: namedtuples
        logger.trace(pickler, "T6: %s", obj)
        if not obj._field_defaults:
            pickler.save_reduce(
                _create_namedtuple, (obj.__name__, obj._fields, obj.__module__), obj=obj
            )
        else:
            defaults = [
                obj._field_defaults[field]
                for field in obj._fields
                if field in obj._field_defaults
            ]
            pickler.save_reduce(
                _create_namedtuple,
                (obj.__name__, obj._fields, obj.__module__, defaults),
                obj=obj,
            )
        logger.trace(pickler, "# T6")
        return

    # special cases: NoneType, NotImplementedType, EllipsisType
    elif obj is type(None):
        logger.trace(pickler, "T7: %s", obj)
        # XXX: pickler.save_reduce(type, (None,), obj=obj)
        pickler.write(bytes("c__builtin__\nNoneType\n", "UTF-8"))
        logger.trace(pickler, "# T7")
    elif obj is NotImplementedType:
        logger.trace(pickler, "T7: %s", obj)
        pickler.save_reduce(type, (NotImplemented,), obj=obj)
        logger.trace(pickler, "# T7")
    elif obj is EllipsisType:
        logger.trace(pickler, "T7: %s", obj)
        pickler.save_reduce(type, (Ellipsis,), obj=obj)
        logger.trace(pickler, "# T7")

    else:
        obj_name = getattr(obj, "__qualname__", getattr(obj, "__name__", None))
        _byref = getattr(pickler, "_byref", None)
        obj_recursive = id(obj) in getattr(pickler, "_postproc", ())
        incorrectly_named = not _locate_function(obj, pickler)
        if (
            not _byref and not obj_recursive and incorrectly_named
        ):  # not a function, but the name was held over
            # thanks to Tom Stepleton pointing out pickler._session unneeded
            logger.trace(pickler, "T2: %s", obj)
            _dict = obj.__dict__.copy()  # convert dictproxy to dict
            # print (_dict)
            # print ("%s\n%s" % (type(obj), obj.__name__))
            # print ("%s\n%s" % (obj.__bases__, obj.__dict__))
            slots = _dict.get("__slots__", ())
            if type(slots) is str:
                slots = (slots,)  # __slots__ accepts a single string
            for name in slots:
                del _dict[name]
            _dict.pop("__dict__", None)
            _dict.pop("__weakref__", None)
            _dict.pop("__prepare__", None)
            if obj_name != obj.__name__:
                if postproc_list is None:
                    postproc_list = []
                postproc_list.append((setattr, (obj, "__qualname__", obj_name)))
            _save_with_postproc(
                pickler,
                (_create_type, (type(obj), obj.__name__, obj.__bases__, _dict)),
                obj=obj,
                postproc_list=postproc_list,
            )
            logger.trace(pickler, "# T2")
        else:
            logger.trace(pickler, "T4: %s", obj)
            if incorrectly_named:
                warnings.warn(
                    "Cannot locate reference to %r." % (obj,), PicklingWarning
                )
            if obj_recursive:
                warnings.warn(
                    "Cannot pickle %r: %s.%s has recursive self-references that "
                    "trigger a RecursionError." % (obj, obj.__module__, obj_name),
                    PicklingWarning,
                )
            # print (obj.__dict__)
            # print ("%s\n%s" % (type(obj), obj.__name__))
            # print ("%s\n%s" % (obj.__bases__, obj.__dict__))
            StockPickler.save_global(pickler, obj, name=obj_name)
            logger.trace(pickler, "# T4")
    return


def save_module(pickler, obj):
    """Adapted from dill._dill.save_module"""
    builtin_mod = _is_builtin_module(obj)
    if (
        obj.__name__ not in ("builtins", "dill", "dill._dill")
        and not builtin_mod
        or is_dill(pickler, child=True)
        and (
            obj is pickler._main
            or filter_patterns(pickler.pickled_modules, obj.__name__)
        )
    ):
        module_dict = obj.__dict__.copy()
        for item in (*singletontypes, "__builtins__", "__loader__"):
            module_dict.pop(item, None)
        mod_name = (
            obj.__name__
            if _is_imported_module(obj)
            else "__runtime__.%s" % obj.__name__
        )
        pickler.save_reduce(create_module, (mod_name,), obj=obj, state=module_dict)
    elif obj.__name__ == "dill._dill":
        logger.trace(pickler, "M2: %s", obj)
        pickler.save_global(obj, name="_dill")
        logger.trace(pickler, "# M2")
    else:
        logger.trace(pickler, "M2: %s", obj)
        pickler.save_reduce(_import_module, (obj.__name__,), obj=obj)
        logger.trace(pickler, "# M2")
    return


def create_module(name, doc=None, file=None):
    """
    Create a module object from a name
    """
    try:
        module = importlib.import_module(name)
    except (ModuleNotFoundError, ImportError):
        module = sys.modules[name] = ModuleType(name, doc)
        module.__file__ = file if file else "<%s>" % name
        module.__builtins__ = __builtins__

    return module


def save_weak_value_dict(pickler, obj):
    pickler.save_reduce(WeakValueDictionary, (), obj=obj)
    return


def save_weak_key_dict(pickler, obj):
    pickler.save_reduce(WeakKeyDictionary, (), obj=obj)
    return


class PretPickler(Pickler):
    dispatch = Pickler.dispatch.copy()

    dispatch[FunctionType] = save_function
    dispatch[TypeType] = save_type
    dispatch[dict] = save_module_dict
    dispatch[WeakValueDictionary] = save_weak_value_dict
    dispatch[WeakKeyDictionary] = save_weak_key_dict
    dispatch[ModuleType] = save_module
    dispatch[CodeType] = save_code_as_source

    def __init__(
        self,
        *args,
        no_recurse_in=None,
        pickled_modules=(),
        save_code_as_source=False,
        **kwds,
    ):
        self.file = io.BytesIO()

        super().__init__(self.file, *args, **kwds)

        self.id = uuid.uuid4().hex
        self.chunk_idx = 0

        self.no_recurse_in = no_recurse_in or {}
        self.pickled_modules = pickled_modules
        self.save_code_as_source = save_code_as_source

        self.modules_dict = {}
        self.saving_modules = set()
        self.saved_modules = set()

        self.accessed_global_refs = set()

    def save(self, obj, save_persistent_id=True):
        try:
            if "_dillable" in obj.__dict__:
                obj = obj._dillable
        except AttributeError:
            pass
        return super().save(obj)

    def dump(self, obj):
        chunk_idx = self.chunk_idx
        super().dump(obj)
        self.chunk_idx += 1
        return self.file.getvalue(), chunk_idx


shared_pickler: Any = None


def get_shared_pickler():
    global shared_pickler
    if shared_pickler is None or shared_pickler() is None:
        pickler = PretPickler(**settings["pickler"])
        shared_pickler = weakref.ref(pickler)
    return shared_pickler()


def clear_shared_pickler():
    global shared_pickler
    shared_pickler = None
