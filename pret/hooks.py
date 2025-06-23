from typing import (
    Any,
    Callable,
    List,
    NewType,
    Optional,
    Tuple,
    TypeVar,
)

from typing_extensions import Protocol

from pret.marshal import js, marshal_as

# from pret.store import (
#     DictPretProxy,
#     ListPretProxy,
#     TrackedDictPretProxy,
#     TrackedListPretProxy,
# )

StateValueType = TypeVar("StateValueType")


@marshal_as(
    js="""
return window.React.useState
"""
)
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


T = TypeVar("T")


@marshal_as(
    js="""
return window.React.useMemo
"""
)
def use_memo(
    fn: "Callable[[], T]",
    dependencies: "List",
) -> "T":
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
    fn: Callable[[], T]
        The function to run to compute the memoized value
    dependencies: List
        The dependencies that will trigger a re-execution of the function

    Returns
    -------
    FunctionReturnType
        The value
    """


RefValueType = TypeVar("RefValueType")


class RefType(Protocol[RefValueType]):
    current: RefValueType


@marshal_as(
    js="""
return window.React.useRef
"""
)
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


C = NewType("C", Callable[..., Any])


@marshal_as(
    js="""
return window.React.useCallback
"""
)
def use_callback(
    callback: "C",
    dependencies: "Optional[List]" = None,
) -> "C":
    """
    Returns a memoized callback function. The callback will be stable across
    re-renders, as long as the dependencies don't change, meaning the last
    callback function passed to this function will be used between two re-renders.

    !!! note

        Ensure that dependencies are simple values like int, str, bool
        to avoid unnecessary re-executions, as these values are converted to
        JavaScript objects, and converting complex objects may not ensure
        referential equality.

    Parameters
    ----------
    callback: C
        The callback function
    dependencies: Optional[List]
        The dependencies that will trigger a re-execution of the callback.

    Returns
    -------

    """
    return js.React.useCallback(callback, dependencies)


def use_effect(effect: "Callable" = None, dependencies: "Optional[List]" = None):
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
    if effect is None:

        def decorator(func):
            return window.React.useEffect(func, dependencies)  # noqa: F821

        return decorator
    # TODO: add window to scoped variables as `js`
    return window.React.useEffect(effect, dependencies)  # noqa: F821


def use_body_style(styles):
    def apply_styles():
        # Remember the original styles
        original_styles = {}
        for key, value in styles.items():
            original_styles[key] = getattr(js.document.documentElement.style, key, "")
            setattr(window.document.documentElement.style, key, value)  # noqa: F821

        # Cleanup function to revert back to the original styles
        def cleanup():
            for k, v in original_styles.items():
                setattr(window.document.documentElement.style, k, v)  # noqa: F821

        return cleanup

    use_effect(apply_styles, [styles])


# @overload
# def use_store_snapshot(
#     proxy_object: "Union[DictPretProxy, TrackedDictPretProxy]",
# ) -> "TrackedDictPretProxy": ...
#
#
# @overload
# def use_store_snapshot(
#     proxy_object: "Union[ListPretProxy, TrackedListPretProxy]",
# ) -> "TrackedListPretProxy": ...


@marshal_as(js="return window.valtio.useSnapshot")
def use_store_snapshot(proxy_object):
    """
    This hook is used to track the access made on a store.
    You cannot use the returned object to change the store, you
    must mutate the original create_store(...) object directly.

    Parameters
    ----------
    proxy_object: ProxyType
        A store object, like the one returned by `create_store({...})`

    Returns
    -------
    TrackedProxyType
        A tracked store object
    """


@marshal_as(
    js="""
return function use_event_callback(callback) {
    const callbackRef = window.React.useRef(callback);
    callbackRef.current = callback;

    return window.React.useCallback(
        (function () {return callbackRef.current(...arguments)}),
        [],
    );
}
"""
)
def use_event_callback(callback: "C"):
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
    callback: C
        The callback function

    Returns
    -------
    C
        The wrapped callback function
    """
