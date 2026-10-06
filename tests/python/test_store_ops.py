"""
Operations on Store values, their inverses and their transforms over concurrent commits
"""

import random

import pytest

from pret.store_ops import Conflict, StoreOpError, apply, clone, transform, transform_seq, validate


def run(state, ops):
    """
    Apply operations to a copy of state and return it with the inverses in the order they undo
    """
    state = clone(state)
    inverse = [apply(state, op) for op in ops]
    return state, inverse[::-1]


def test_apply_and_inverse_of_each_operation():
    """
    Each operation kind changes maps and lists in place and its inverse restores the previous value
    """
    state = {"data": {"title": "Todos", "todos": [{"text": "milk"}, {"text": "eggs"}]}}
    ops = [
        {"op": "set", "path": ["data", "title"], "value": "Groceries"},
        {"op": "set", "path": ["data", "owner"], "value": "Michael"},
        {"op": "insert", "path": ["data", "todos", 1], "value": {"text": "bread"}},
        {"op": "delete", "path": ["data", "todos", 0]},
        {"op": "set", "path": ["data", "todos", 1, "text"], "value": "cheese"},
        {"op": "delete", "path": ["data", "title"]},
    ]
    after, inverse = run(state, ops)
    assert after == {"data": {"owner": "Michael", "todos": [{"text": "bread"}, {"text": "cheese"}]}}
    assert run(after, inverse)[0] == state
    with pytest.raises(StoreOpError):
        apply(clone(state), {"op": "delete", "path": ["data", "missing"]})
    with pytest.raises(StoreOpError):
        apply(clone(state), {"op": "insert", "path": ["data", "todos", 5], "value": 1})
    with pytest.raises(StoreOpError, match="splices"):
        apply(clone(state), {"op": "splice", "path": ["data", "title"], "index": 0, "delete": 1, "text": "t"})


def test_values_are_validated():
    """
    Store values stay JSON values a browser represents exactly
    """
    assert validate({"a": (1, 2.5, None, True)}) == {"a": [1, 2.5, None, True]}
    for value in (float("nan"), float("inf"), 2**53, b"bytes", {1: "a"}, {1, 2}):
        with pytest.raises((TypeError, ValueError)):
            validate(value)


@pytest.mark.parametrize(
    "a,b,a_later,expected,flags",
    [
        # Inserts before a list position shift it, the committed insert stays first at the same position
        (["L", 2, "x"], ("insert", ["L", 1]), True, ["L", 3, "x"], set()),
        (("insert", ["L", 1]), ("insert", ["L", 1]), True, ["L", 2], set()),
        (("insert", ["L", 1]), ("insert", ["L", 1]), False, ["L", 1], set()),
        (["L", 0], ("insert", ["L", 1]), True, ["L", 0], set()),
        # Deletes shift the following positions, deleting the same element drops the second delete
        (["L", 2], ("delete", ["L", 1]), True, ["L", 1], set()),
        (("delete", ["L", 1]), ("delete", ["L", 1]), True, None, {"drop", "touch"}),
        (["L", 1, "x"], ("delete", ["L", 1]), True, None, {"invalid", "touch"}),
        (("insert", ["L", 1]), ("delete", ["L", 1]), True, ["L", 1], set()),
        # Same key writes keep the last writer, edits under a replaced value are invalid
        (["M", "k"], ("set", ["M", "k"]), True, ["M", "k"], {"touch"}),
        (["M", "k", "x"], ("set", ["M", "k"]), True, None, {"invalid", "touch"}),
        (["M"], ("set", ["M", "k"]), True, ["M"], {"touch"}),
        (("delete", ["M", "k"]), ("delete", ["M", "k"]), True, None, {"drop", "touch"}),
        (["M", "j"], ("set", ["M", "k"]), True, ["M", "j"], set()),
    ],
)
def test_transform_cases(a, b, a_later, expected, flags):
    """
    One case per rule of transform, a is a set unless given as (kind, path)
    """
    a = {"op": a[0], "path": a[1], "value": 0} if isinstance(a, tuple) else {"op": "set", "path": a, "value": 0}
    b = {"op": b[0], "path": b[1], "value": 0}
    moved, found = transform(a, b, a_later)
    assert (moved and moved["path"]) == expected and found == flags


def test_rebase_of_concurrent_pushes_and_conflicts():
    """
    Two pages pushing to the same list from the same revision keep both items, in commit order
    """
    state = {"data": {"todos": [], "lists": {"a": 1}}}
    committed = [{"op": "insert", "path": ["data", "todos", 0], "value": "first", "origin": "page-1"}]
    late = [{"op": "insert", "path": ["data", "todos", 0], "value": "second"}]
    after = run(state, committed)[0]
    assert run(after, transform_seq(late, committed))[0]["data"]["todos"] == ["first", "second"]
    removed = [{"op": "delete", "path": ["data", "lists"], "origin": "page-1"}]
    with pytest.raises(Conflict):
        transform_seq([{"op": "set", "path": ["data", "lists", "a"], "value": 2}], removed)


def test_undo_skips_values_changed_by_other_origins():
    """
    Reversing a commit keeps the later edits of other pages and restores the rest
    """
    state = {"data": {"title": "a", "count": 0, "todos": ["x"]}}
    edit = [
        {"op": "set", "path": ["data", "title"], "value": "b"},
        {"op": "set", "path": ["data", "count"], "value": 1},
        {"op": "insert", "path": ["data", "todos", 1], "value": "y"},
    ]
    after, inverse = run(state, edit)
    peer = [
        {"op": "set", "path": ["data", "count"], "value": 1, "origin": "peer"},
        {"op": "insert", "path": ["data", "todos", 0], "value": "w", "origin": "peer"},
    ]
    after = run(after, peer)[0]
    reversal = transform_seq(inverse, peer, mode="undo", origin="page")
    assert run(after, reversal)[0]["data"] == {"title": "a", "count": 1, "todos": ["w", "x"]}


def random_value(rng, depth=0):
    """
    Build a small random JSON value for the property tests
    """
    kind = rng.random()
    if depth < 2 and kind < 0.3:
        return {rng.choice("abc"): random_value(rng, depth + 1) for _ in range(rng.randint(0, 2))}
    if depth < 2 and kind < 0.6:
        return [random_value(rng, depth + 1) for _ in range(rng.randint(0, 2))]
    return rng.choice([None, True, 1, 2.5, "s"])


def random_op(rng, state):
    """
    Pick a valid operation on a random container of state
    """
    containers = []

    def walk(value, path):
        if isinstance(value, (dict, list)):
            containers.append((value, path))
            items = value.items() if isinstance(value, dict) else enumerate(value)
            for key, child in items:
                walk(child, [*path, key])

    walk(state["data"], ["data"])
    container, path = rng.choice(containers)
    if isinstance(container, dict):
        if container and rng.random() < 0.3:
            return {"op": "delete", "path": [*path, rng.choice(list(container))]}
        return {"op": "set", "path": [*path, rng.choice("abc")], "value": random_value(rng)}
    choice = rng.random()
    if container and choice < 0.3:
        return {"op": "delete", "path": [*path, rng.randrange(len(container))]}
    if container and choice < 0.5:
        return {"op": "set", "path": [*path, rng.randrange(len(container))], "value": random_value(rng)}
    return {"op": "insert", "path": [*path, rng.randint(0, len(container))], "value": random_value(rng)}


def random_ops(rng, state, count):
    """
    Build a valid sequence of operations by applying each to a copy of state
    """
    state = clone(state)
    ops = []
    for _ in range(count):
        op = random_op(rng, state)
        apply(state, op)
        ops.append(op)
    return ops


@pytest.mark.parametrize("seed", range(200))
def test_random_inverses_and_rebased_operations_apply(seed):
    """
    Inverses restore the value, and rebased or reversed sequences always apply when no conflict is raised
    """
    rng = random.Random(seed)
    state = {"data": {"a": [1, {"b": [2, 3]}], "b": {"c": "x"}}}
    mine = random_ops(rng, state, rng.randint(1, 4))
    after, inverse = run(state, mine)
    assert run(after, inverse)[0] == state
    theirs = [{**op, "origin": "peer"} for op in random_ops(rng, state, rng.randint(1, 4))]
    committed = run(state, theirs)[0]
    try:
        rebased = transform_seq(mine, theirs)
    except Conflict:
        rebased = None
    if rebased is not None:
        run(committed, rebased)
    later = [{**op, "origin": "peer"} for op in random_ops(rng, after, rng.randint(1, 4))]
    run(run(after, later)[0], transform_seq(inverse, later, mode="undo", origin="page"))
