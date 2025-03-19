import functools
from typing import (
    Any,
    Callable,
    List,
    NewType,
    Optional,
    Protocol,
    Tuple,
    TypeVar,
    Union,
    overload,
)

from pret.state import (
    DictPretProxy,
    ListPretProxy,
    TrackedDictPretProxy,
    TrackedListPretProxy,
    get_untracked,
    is_changed,
    is_proxy,
    snapshot,
    subscribe,
    tracked,
)

from .bridge import create_proxy, js, pyodide, to_js

StateValueType = TypeVar("StateValueType")


def use_state(
    initial_value: "StateValueType",
) -> "Tuple[StateValueType, Callable[[StateValueType], None]]":
    """
    Returns a stateful value, and a function to update it.

    Examples
    --------

    ```python
    from pret.ui.react import div, button, p
    from pret import component, use_state


    @component
    def CounterApp():
        count, set_count = use_state(0)

        def increment():
            set_count(count + 1)

        return div(p(count), button({"onClick": increment}, "Increment"))
    ```

    Parameters
    ----------
    initial_value: StateValueType
        The initial value of the state

    Returns
    -------
    Tuple[StateValueType, Callable[[StateValueType], None]]

        - The current value of the state
        - A function to update the state
    """
    value, set_value = js.React.useState(pyodide.ffi.create_proxy(initial_value))

    def set_proxy_value(new_value):
        return set_value(pyodide.ffi.create_proxy(new_value))

    return value.unwrap(), set_proxy_value


FunctionReturnType = TypeVar("FunctionReturnType")


def use_memo(
    fn: "Callable[[], FunctionReturnType]",
    dependencies: "List",
) -> "FunctionReturnType":
    """
    Returns a memoized value, computed from the provided function.
    The function will only be re-executed if any of the dependencies change.

    !!! note

        Ensure that dependencies are simple values like int, str, bool
        to avoid unnecessary re-executions, as these values are converted to
        javascript objects, and converting complex objects may not ensure
        referential equality.

    Parameters
    ----------
    fn: Callable[[], Any]
        The function to run to compute the memoized value
    dependencies: List
        The dependencies that will trigger a re-execution of the function

    Returns
    -------
    FunctionReturnType
        The value
    """
    return js.React.useMemo(fn, dependencies)


RefValueType = TypeVar("RefValueType")


class RefType(Protocol[RefValueType]):
    current: RefValueType


def use_ref(initial_value: "RefValueType") -> "RefType[RefValueType]":
    """
    Returns a mutable ref object whose `.current` property is initialized to the
    passed argument.

    The returned object will persist for the full lifetime of the component.

    Parameters
    ----------
    initial_value: Any
        The initial value of the ref

    Returns
    -------
    RefType[RefValueType]
        The ref object
    """
    return js.React.useRef(initial_value)


CallbackType = NewType("CallbackType", Callable[..., Any])


def use_callback(
    callback: "CallbackType",
    dependencies: "Optional[List]" = None,
) -> "CallbackType":
    """
    Returns a memoized callback function. The callback will be stable across
    re-renders, as long as the dependencies don't change, meaning the last
    callback function passed to this function will be used between two re-renders.

    !!! note

        Ensure that dependencies are simple values like int, str, bool
        to avoid unnecessary re-executions, as these values are converted to
        javascript objects, and converting complex objects may not ensure
        referential equality.

    Parameters
    ----------
    callback: CallbackType
        The callback function
    dependencies: Optional[List]
        The dependencies that will trigger a re-execution of the callback.

    Returns
    -------

    """
    return js.React.useCallback(callback, dependencies)


def use_effect(effect: "Callable", dependencies: "Optional[List]" = None):
    """
    The `useEffect` hook allows you to perform side effects in function components.
    Side effects can include data fetching, subscriptions, manually changing the DOM,
    and more.

    The effect runs after every render by default. If `dependencies` are provided,
    the effect runs whenever those values change. Therefore, if `dependencies` is an
    empty array, the effect runs only once after the initial render.

    !!! note

        Ensure that dependencies are simple values like int, str, bool
        to avoid unnecessary re-executions, as these values are converted to
        javascript objects, and converting complex objects may not ensure
        referential equality.

    Parameters
    ----------
    effect: Callable
        A function containing the side effect logic.
        It can optionally return a cleanup function.
    dependencies: Optional[List]
        An optional array of dependencies that determines when the effect runs.
    """
    effect = pyodide.ffi.create_once_callable(effect)
    return js.React.useEffect(effect, pyodide.ffi.to_js(dependencies))


def use_body_style(styles):
    def apply_styles():
        # Remember the original styles
        original_styles = {}
        for key, value in styles.items():
            original_styles[key] = getattr(js.document.documentElement.style, key, "")
            setattr(js.document.documentElement.style, key, value)

        # Cleanup function to revert back to the original styles
        def cleanup():
            for k, v in original_styles.items():
                setattr(js.document.documentElement.style, k, v)

        return cleanup

    use_effect(pyodide.ffi.create_once_callable(apply_styles), [str(styles)])


@overload
def use_tracked(
    proxy_object: "Union[DictPretProxy, TrackedDictPretProxy]",
) -> "TrackedDictPretProxy": ...


@overload
def use_tracked(
    proxy_object: "Union[ListPretProxy, TrackedListPretProxy]",
) -> "TrackedListPretProxy": ...


def use_tracked(proxy_object):
    """
    This hook is used to track the access made on a proxy object.
    You can also use the returned object to change the proxy object.

    Parameters
    ----------
    proxy_object: ProxyType
        A proxy object, like the one returned by `proxy({...})`

    Returns
    -------
    TrackedProxyType
        A tracked proxy object
    """
    proxy_object = get_untracked(proxy_object)
    if not is_proxy(proxy_object):
        raise ValueError("use_tracked can only be used with proxy objects")
    last_snapshot = js.React.useRef(None)
    last_affected = js.React.useRef(None)
    in_render = True

    def external_store_subscribe(callback):
        unsub = subscribe(proxy_object, callback, notify_in_sync=False)
        return unsub

    def external_store_get_snapshot():
        try:
            next_snapshot = snapshot(proxy_object)
        except KeyError:
            return js.undefined
        if not in_render and last_snapshot.current and last_affected.current:
            if not is_changed(
                last_snapshot.current["wrapped"], next_snapshot, last_affected.current
            ):
                return last_snapshot.current

        res = to_js(next_snapshot, wrap=True)
        return res

    # we don't use lambda's because of serialization issues
    def make_proxied_external_store_subscribe():
        return create_proxy(external_store_subscribe)

    def make_proxied_external_store_get_snapshot():
        return create_proxy(external_store_get_snapshot)

    curr_snapshot = js.React.useSyncExternalStore(
        js.React.useMemo(
            make_proxied_external_store_subscribe,
            pyodide.ffi.to_js([id(proxy_object)]),
        ),
        js.React.useMemo(
            make_proxied_external_store_get_snapshot,
            pyodide.ffi.to_js([id(proxy_object)]),
        ),
    )

    in_render = False
    curr_affected = dict()

    def side_effect():
        last_snapshot.current = curr_snapshot
        last_affected.current = curr_affected

    # No dependencies, will run once after each render -> create_once_callable
    js.React.useEffect(pyodide.ffi.create_once_callable(side_effect))
    return tracked(curr_snapshot["wrapped"], curr_affected, proxy_object)


def use_event_callback(callback: "CallbackType"):
    """
    This hook is used to store a callback function that will be called when an event
    is triggered. The callback function can be changed without triggering a re-render
    of the component. The function returns a wrapped callback function that will in
    turn call the stored callback function.

    !!! warning
        Do not use this hook if the rendering of the component depends on the callback
        function.

    Parameters
    ----------
    callback: CallbackType
        The callback function

    Returns
    -------
    CallbackType
        The wrapped callback function
    """
    ref = js.React.useRef(callback)

    def side_effect():
        ref.current = callback

    once_callable = pyodide.ffi.create_once_callable(side_effect)
    once_callable.displayName = "use_event_callback"
    js.React.useEffect(once_callable)

    @functools.wraps(callback)
    def wrapped(*args, **kwargs):
        return ref.current(*args, **kwargs)

    return wrapped
