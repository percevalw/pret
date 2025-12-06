import functools
import uuid
from typing import (
    Any,
    Callable,
    List,
    Optional,
    Tuple,
    TypeVar,
)

from typing_extensions import Protocol

from .manager import get_manager
from .marshal import js, marshal_as

T = TypeVar("T")


@marshal_as(
    js="""
return window.React.useState
"""
)
def use_state(
    initial_value: T,
) -> "Tuple[T, Callable[[T | Callable[[T], T]], None]]":
    """
    Returns a stateful value, and a function to update it.

    Examples
    --------

    ```python
    from pret import component, use_state
    from pret.react import div, button, p


    @component
    def CounterApp():
        count, set_count = use_state(0)

        def increment():
            set_count(count + 1)

        return div(p(count), button({"onClick": increment}, "Increment"))
    ```

    Parameters
    ----------
    initial_value: T
        The initial value of the state

    Returns
    -------
    Tuple[T, Callable[T | Callable[[T], T], None]]

        - The current value of the state
        - A function to update the state
    """


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


def make_client_ref(_id):
    ref = window.React.createRef()  # noqa: F821
    get_manager().register_ref(_id, ref)
    return ref


class RemoteRefCurrent:
    def __init__(self, _id):
        self._id = _id

    def _remote_call(self, method_name, *args, **kwargs):
        return get_manager().remote_call_ref_method(self._id, method_name, args, kwargs)

    def __getattr__(self, method_name):
        return functools.partial(self._remote_call, method_name)


class RemoteRef:
    def __init__(self, current: RemoteRefCurrent):
        self.current = current

    def __reduce__(self):
        return make_client_ref, (self.current._id,)


class RefType(Protocol[T]):
    current: T


@marshal_as(
    js="""
return window.React.useRef
"""
)
def use_ref(initial_value: "T" = None) -> "RefType[T]":
    """
    Returns a mutable ref object whose `.current` property is initialized to the
    passed argument.

    The returned object will persist for the full lifetime of the component.

    If called from the Python kernel/server side, it will create a RemoteRef object,
    which can only be used to call methods/getters on the remote ref (no property
    access) and get the results as futures.

    Visit the [Widgets](/tutorials/widgets) tutorial to learn more about using
    refs in Pret.

    !!! note "`current` Property"

        Because refs are mutable containers whose contents can change without causing
        re-renders, React stores the actual value in a .current property so the ref
        object itself stays stable while its contents update freely.

        To keep this behavior consistent between server and client side calls to
        `use_ref`, the value of a ref is always accessed through the `.current`
        property.

    Parameters
    ----------
    initial_value: Any
        The initial value of the ref

    Returns
    -------
    RefType[T]
        The ref object
    """
    return RemoteRef(RemoteRefCurrent(uuid.uuid4().hex))


@marshal_as(js="return window.React.useImperativeHandle")
def use_imperative_handle(
    ref: Optional[RefType[T]],
    create_handle: Callable[[], T],
    dependencies: Optional[List] = None,
) -> None:
    """
    Safely binds a custom value or API to a parent's ref.

    Unlike manually setting `ref.current`, this hook automatically handles the
    lifecycle: it updates the ref when dependencies change and cleans it up
    (resets it to `None`) when the component unmounts.

    Use this to expose specific methods (like `focus` or `reset`) to a parent, rather
    than giving them direct access to internal DOM nodes.

    Example
    -------

    ```python
    from pret import component, use_ref, use_imperative_handle
    from pret.react import button, div, input


    @component
    def CustomInput(handle):
        # 1. The internal ref connects to the actual DOM element
        internal_input_ref = use_ref(None)

        # 2. We define what the parent is allowed to see/do and attach
        # that API to the handle passed down from the parent
        use_imperative_handle(
            handle,
            lambda: {
                "reset_and_focus": lambda: (
                    setattr(internal_input_ref.current, "value", ""),
                    internal_input_ref.current.focus(),
                )
            },
            [],
        )

        return input(placeholder="Type here...", ref=internal_input_ref)


    @component
    def App():
        # The parent creates a ref
        input_controller = use_ref(None)

        def on_click(event):
            # The parent calls the custom method defined in the child
            input_controller.current.reset_and_focus()

        return div(
            # Pass the ref as a regular prop named 'handle'
            CustomInput(handle=input_controller),
            button("Reset Form", onClick=on_click),
        )
    ```

    Visit the [Widgets](/tutorials/widgets) tutorial to learn more about using
    `use_imperative_handle` in Pret.

    Parameters
    ----------
    ref: RefType | None
        The ref object passed in from the parent component (via a prop).
    create_handle: Callable[[], Any]
        A function that returns the custom object/API to be assigned to `ref.current`.
    dependencies: List | None
        Optional dependencies. If these change, the handle is re-created.
    """
    return js.React.useImperativeHandle(ref, create_handle, dependencies)


C = TypeVar("C", bound=Callable[..., Any])


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

    Parameters
    ----------
    callback: C
        The callback function
    dependencies: List | None
        The dependencies that will trigger a re-assignment of the callback.

    Parameters
    ----------
    callback: C
        The callback function
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


@marshal_as(js="return window.storeLib.useSnapshot")
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
return function use_event_callback(callback, dependencies) {
    const callbackRef = window.React.useRef(callback);
    callbackRef.current = callback;

    return window.React.useCallback(
        (function () {return callbackRef.current(...arguments)}),
        dependencies,
    );
}
"""
)
def _use_event_callback_impl(callback: "C", dependencies: "Optional[List]" = None) -> "C":
    """Internal JS-implemented hook. Prefer using :func:`use_event_callback`."""


def use_event_callback(arg: "Optional[Any]" = None):
    """
    Store a callback function that can be updated without re-rendering, and return a
    stable wrapped function that will always call the latest callback.

    This can be used in two ways:

    1) With dependencies (decorator factory):

    ```python
    @use_event_callback([dep1, dep2])
    def the_callback(*args, **kwargs):
        ...
    ```

    2) or without

    ```python
    @use_event_callback  # (equivalent to @use_event_callback([]))
    def the_callback(*args, **kwargs):
        ...
    ```

    The optional `dependencies` behave like the dependency array of React's
    `useCallback`. If an empty list is provided, the wrapped callback will be called
    only once (when the component is mounted).
    If None is provided, the wrapped callback will be called on every render.

    !!! warning
        Do not use this hook if the rendering of the component depends on the callback
        function.

    Parameters
    ----------
    arg: Callable | List | None
        Either the callback itself (bare decorator / direct call), or a dependency
        list when used as `@use_event_callback([...])`.

    Returns
    -------
    Callable
        The wrapped callback function (or a decorator that returns it).
    """

    if callable(arg):
        return _use_event_callback_impl(arg, [])

    def decorator(callback: "C") -> "C":
        return _use_event_callback_impl(callback, arg)

    return decorator
