import base64
import importlib
import io
import os
import sys
from weakref import WeakValueDictionary

if not os.path.isfile(os.devnull):
    with open("dill_handle", "w") as f:
        f.write("")
    os.devnull = "dill_handle"

from dill._dill import Unpickler as DillUnpickler

unpicklers = WeakValueDictionary()
chunks = dict()


def create_module(name, doc=None, file=None):
    """create a module object from a name"""
    try:
        module = importlib.import_module(name)
    except (ModuleNotFoundError, ImportError):
        module = sys.modules[name] = type(sys)(name, doc)
        module.__file__ = file if file else "<%s>" % name
        module.__builtins__ = __builtins__

    return module


def create_module_with_dict(name, module_dict, doc=None, file=None):
    """create a module with the given name, docstring, and optional file name"""
    path_parts = name.split(".")
    for i in range(1, len(path_parts)):
        pname = ".".join(path_parts[:i])
        try:
            importlib.import_module(pname)
        except (ModuleNotFoundError, ImportError):
            pmodule = sys.modules[pname] = type(sys)(pname, "")
            pmodule.__file__ = pname
            pmodule.__builtins__ = __builtins__

    try:
        module = importlib.import_module(name)
    except (ModuleNotFoundError, ImportError):
        module = sys.modules[name] = type(sys)(name, doc)
        module.__file__ = file if file else "<%s>" % name
        module.__builtins__ = __builtins__

    module.__dict__.update(module_dict)

    return module.__dict__


def create_code_from_source(source_code):
    var_dict = {}
    exec(source_code, var_dict, var_dict)
    return var_dict["_fn_"].__code__


create_module_with_dict(
    "pret.serialize",
    {
        "create_module_with_dict": create_module_with_dict,
        "create_module": create_module,
        "create_code_from_source": create_code_from_source,
    },
)


class Unpickler(DillUnpickler):
    """python's Unpickler extended to interpreter sessions and more types"""

    def __init__(
        self, *, fix_imports=True, encoding="ASCII", errors="strict", buffers=None
    ):
        self.file = io.BytesIO()
        super().__init__(
            self.file,
            fix_imports=fix_imports,
            encoding=encoding,
            errors=errors,
            buffers=buffers,
        )

    def load_data(
        self, file, buffers=None
    ):  # NOTE: if settings change, need to update attributes
        # Add the content of file to self.file
        pos = self.file.tell()
        self.file.seek(0)
        self.file.truncate()
        self.file.write(file.read())
        self.file.seek(pos)
        data_chunks = []
        pos = 0
        while self.file.tell() < self.file.getbuffer().nbytes:
            data_chunks.append((self.load(), self.file.tell() - pos))
            pos = self.file.tell()
        return data_chunks


def load_view(data, unpickler_id, chunk_idx):
    print(f"Loading view with unpickled ID {unpickler_id} and chunk ID {chunk_idx}")
    if unpickler_id not in chunks:
        chunks[unpickler_id] = WeakValueDictionary()
    if chunk_idx in chunks[unpickler_id]:
        print("Reusing chunk", chunk_idx)
        result = chunks[unpickler_id][chunk_idx]
        return (result, result._manager)

    if unpickler_id not in unpicklers:
        print("Creating new unpickler", unpickler_id)
        unpickler = unpicklers[unpickler_id] = Unpickler()
    else:
        print("Reusing unpickler", unpickler_id)
        unpickler = unpicklers[unpickler_id]

    print(f"Loading {len(data)} bytes")
    try:
        file = io.BytesIO(base64.decodebytes(data.encode()))

        next_chunk_idx = len(chunks[unpickler_id])

        for result, size in unpickler.load_data(file):
            result[0]._unpickler = unpickler
            result[0]._manager = result[1]
            chunks[unpickler_id][next_chunk_idx] = result[0]
            print("Unpacked chunk", next_chunk_idx, "of size", size)
            next_chunk_idx += 1

        result = (
            chunks[unpickler_id][chunk_idx],
            chunks[unpickler_id][chunk_idx]._manager,
        )
    except Exception as e:
        print("Exception while loading", e)
        import traceback

        traceback.print_exc()
        return [None, None]

    # PyProxy will be created here holding a reference for the chunks collection
    return result


print("Done loading unpacking script")

load_view
