"""
JSON operations on Store values, their inverses, and their transforms over concurrent operations.

Ordinary Store values are a JSON tree. Every edit is a list of operations on paths from the root, e.g.

```text
{"op": "set", "path": ["data", "todos", 0, "done"], "value": true}
{"op": "insert", "path": ["data", "todos", 2], "value": {"text": "Buy milk", "done": false}}
{"op": "delete", "path": ["data", "filters", "label"]}
```

Map keys are str and list indices int, so the last path element tells whether an operation targets a map key
or a list position. Python applies browser operations over the commits they did not see with `transform_seq`,
and reverses a commit with its inverse operations transformed the same way, see pret/store.py.
"""

import math
import numbers
from typing import Any, Callable, List, Optional, Sequence, Tuple

MISSING = object()
MAX_SAFE_INTEGER = 2**53 - 1


class StoreOpError(ValueError):
    """
    An operation does not apply to the current Store value, e.g. its path no longer exists
    """


class Conflict(ValueError):
    """
    A browser operation targets a value that a later commit removed or replaced
    """


def validate(value: Any) -> Any:
    """
    Return a plain JSON copy of a value assigned to a Store, refusing values a browser cannot represent.

    Tuples become lists, numbers outside the exact JavaScript integer range and non finite floats are refused.
    """
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, numbers.Integral):
        if abs(int(value)) > MAX_SAFE_INTEGER:
            raise ValueError(f"Store integers must be within ±{MAX_SAFE_INTEGER}, got {value}")
        return int(value)
    if isinstance(value, numbers.Real):
        if not math.isfinite(value):
            raise ValueError(f"Store numbers must be finite, got {value}")
        return float(value)
    if isinstance(value, dict) or (hasattr(value, "keys") and hasattr(value, "__getitem__")):
        result = {}
        for key in value.keys():
            if not isinstance(key, str):
                raise TypeError(f"Store mapping keys must be str, got {key!r}")
            result[key] = validate(value[key])
        return result
    if isinstance(value, (list, tuple)) or (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
    ):
        return [validate(item) for item in value]
    raise TypeError(f"Store values must be JSON values or DataFrames, got {type(value).__name__}")


def clone(value: Any) -> Any:
    """
    Copy a JSON value, containers included
    """
    if isinstance(value, dict):
        return {key: clone(child) for key, child in value.items()}
    if isinstance(value, list):
        return [clone(child) for child in value]
    return value


def resolve(root: Any, path: Sequence) -> Tuple[Any, Any]:
    """
    Return the container holding the last element of path, and that key or index
    """
    if not path:
        raise StoreOpError("Operations need a non empty path")
    container = root
    for part in path[:-1]:
        container = child_at(container, part)
    return container, path[-1]


def child_at(container: Any, part: Any) -> Any:
    """
    Read a map key or list index along an operation path, refusing a path that no longer exists
    """
    if isinstance(container, dict) and isinstance(part, str):
        if part not in container:
            raise StoreOpError(f"Missing key {part!r}")
        return container[part]
    if isinstance(container, list) and isinstance(part, int) and not isinstance(part, bool):
        if not 0 <= part < len(container):
            raise StoreOpError(f"Index {part} outside a list of {len(container)} items")
        return container[part]
    raise StoreOpError(f"Path element {part!r} does not match a {type(container).__name__}")


def apply(root: Any, op: dict, make: Optional[Callable[[Any, Any], Any]] = None) -> dict:
    """
    Apply one operation in place and return its inverse.

    make builds the tree value from the operation value and its new container, the live Store uses it to
    create nodes, a copy is stored otherwise. The inverse holds the removed objects themselves, so applying
    it restores the same containers.
    """
    kind, path = op["op"], op["path"]
    container, key = resolve(root, path)
    make = make or (lambda value, container: clone(value))
    if kind == "set":
        if isinstance(container, dict) and isinstance(key, str):
            old = container.get(key, MISSING)
            container[key] = make(op["value"], container)
            return {"op": "delete", "path": path} if old is MISSING else {"op": "set", "path": path, "value": old}
        if isinstance(container, list) and isinstance(key, int) and 0 <= key < len(container):
            old = container[key]
            container[key] = make(op["value"], container)
            return {"op": "set", "path": path, "value": old}
    elif kind == "delete":
        if isinstance(container, dict) and isinstance(key, str) and key in container:
            return {"op": "set", "path": path, "value": container.pop(key)}
        if isinstance(container, list) and isinstance(key, int) and 0 <= key < len(container):
            return {"op": "insert", "path": path, "value": container.pop(key)}
    elif kind == "insert":
        if isinstance(container, list) and isinstance(key, int) and 0 <= key <= len(container):
            container.insert(key, make(op["value"], container))
            return {"op": "delete", "path": path}
    elif kind == "splice":
        raise StoreOpError("Text splices are not supported yet")
    else:
        raise StoreOpError(f"Unknown Store operation {kind!r}")
    raise StoreOpError(f"Cannot {kind} at {path!r}")


def is_list_position(op: dict) -> bool:
    """
    Tell whether the operation inserts or removes a list element, which shifts the following indices
    """
    return op["op"] in ("insert", "delete") and isinstance(op["path"][-1], int)


def transform(a: dict, b: dict, a_later: bool) -> Tuple[Optional[dict], set]:
    """
    Express a, defined on the same value as b, after b was applied.

    Returns the moved operation, or None when a no longer applies, and flags among "drop" (b already did
    what a does), "invalid" (b removed or replaced the value a targets) and "touch" (both changed the same
    value). a_later decides which of two inserts at the same position goes first.
    """
    pa, pb = list(a["path"]), list(b["path"])
    if is_list_position(b):
        parent, i = pb[:-1], pb[-1]
        n = len(parent)
        if len(pa) > n and pa[:n] == parent and isinstance(pa[n], int):
            j = pa[n]
            at_slot = len(pa) == n + 1
            if b["op"] == "insert":
                if j > i or (j == i and not (a["op"] == "insert" and at_slot)):
                    return {**a, "path": [*parent, j + 1, *pa[n + 1 :]]}, set()
                if j == i and a_later:
                    return {**a, "path": [*parent, j + 1, *pa[n + 1 :]]}, set()
                return a, set()
            if j > i:
                return {**a, "path": [*parent, j - 1, *pa[n + 1 :]]}, set()
            if j == i:
                if at_slot and a["op"] == "insert":
                    return a, set()
                if at_slot and a["op"] == "delete":
                    return None, {"drop", "touch"}
                return None, {"invalid", "touch"}
            return a, set()
        if len(pa) <= n and pb[: len(pa)] == pa and a["op"] != "insert":
            # a replaces or removes a container holding the list b changed
            return a, {"touch"}
        return a, set()
    if len(pa) < len(pb) and pb[: len(pa)] == pa:
        return a, (set() if a["op"] == "insert" else {"touch"})
    if pa == pb:
        if a["op"] == "insert":
            return a, set()
        if a["op"] == "delete" and b["op"] == "delete":
            return None, {"drop", "touch"}
        return a, {"touch"}
    if len(pb) < len(pa) and pa[: len(pb)] == pb:
        return None, {"invalid", "touch"}
    return a, set()


def shape_inverse(op: dict) -> dict:
    """
    Return an operation with the opposite effect on list positions, to move later operations past a skipped one
    """
    if is_list_position(op):
        return {"op": "delete" if op["op"] == "insert" else "insert", "path": op["path"], "value": None}
    return {"op": "set", "path": op["path"], "value": None}


def transform_seq(ops: List[dict], committed: List[dict], mode: str = "rebase", origin: Any = None) -> List[dict]:
    """
    Move a sequence of operations over committed operations that it did not see.

    committed items are operations with the "origin" of their commit. In "rebase" mode, used for browser
    requests, an operation whose target was removed or replaced raises Conflict and the last writer wins
    otherwise. In "undo" mode, used to reverse a commit, operations whose target another origin changed
    later are skipped, so the reversal keeps the edits of other pages.
    """
    ops = list(ops)
    for c in committed:
        moved = []
        k = 0
        while k < len(ops):
            x = ops[k]
            if c is None:
                moved.append(x)
                k += 1
                continue
            x2, fx = transform(x, c, a_later=True)
            c2, _ = transform(c, x, a_later=False)
            skip = "invalid" in fx or (mode == "undo" and "touch" in fx and c["origin"] != origin)
            if "invalid" in fx and mode == "rebase":
                raise Conflict(f"Operation at {x['path']!r} targets a value changed by a newer commit")
            if "drop" in fx:
                c = None
                k += 1
                continue
            if skip:
                # The remaining operations were defined after x, move them as if x had been undone
                rest = transform_seq(ops[k + 1 :], [{**shape_inverse(x), "origin": origin}], mode, origin)
                ops = moved + rest
                k = len(moved)
                continue
            moved.append(x2)
            c = None if c2 is None else {**c2, "origin": c["origin"]}
            k += 1
        ops = moved
    return ops
