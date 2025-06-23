"""
This module provides helpers to create React components from Python functions,
and to create components from existing React components.
Any component is made renderable in Jupyter by wrapping it in a `Renderable` object
when called from Python.
"""

import functools
from typing import Callable, TypeVar

from pret.manager import get_manager
from pret.marshal import get_shared_marshaler, marshal_as
from pret.store import snapshot

T = TypeVar("T")


def stub_component(name, props_mapping) -> Callable[[T], T]:
    def make(fn):
        @marshal_as(
            js="""
return function py_to_react() {
    var children;
    var props;
	if (
        arguments.length > 0
        && arguments[arguments.length - 1]
        && arguments[arguments.length - 1].hasOwnProperty("__kwargtrans__")
    ) {
        children = Array.prototype.slice.call(arguments, 0, -1);
        props = arguments[arguments.length - 1];
        delete props.__kwargtrans__;
        var props = Object.fromEntries(Object.entries(props).map(([k, v]) => [
            props_mapping[k] || k,
            snapshot(v)
        ]));
    } else {
        children = Array.from(arguments);
        props = {};
    }
    return window.React.createElement(
        name,
        props,
        ...(Array.isArray(children) ? children : [children])
    );
}
""",
            globals={
                "name": name,
                "props_mapping": props_mapping,
                "snapshot": snapshot,
            },
        )
        def py_to_react(*children, **props): ...

        @functools.wraps(fn)
        @marshal_as(py_to_react)
        def wrapped(*children, detach=False, **props):
            def render():
                return py_to_react(*children, **props)

            return Renderable(
                render,
                detach=detach,
            )

        return wrapped

    return make


def make_create_element_from_function(fn):
    """
    Turn a Python Pret function into function that creates a React element.

    Parameters
    ----------
    fn: Callable
        The Python function to turn into a React element creator, ie a function
        that when invoked by React, will call the Python function with the
        correct arguments.

    Returns
    -------
    (**props) -> ReactElement<fn
    """

    @marshal_as(
        js="""
return function react_to_py(props) {
    var children = props.children || {};
    var rest = Object.fromEntries(
        Object.entries(props).filter(([key, _]) => key !== "children")
    );
    return fn(...Object.values(props.children || {}), __kwargtrans__(rest));
}
""",
        globals={"fn": fn},
    )
    def react_to_py(props, ctx=None): ...

    @marshal_as(
        js="""
// py_to_react for @component
return function py_to_react() {
	if (
        arguments.length > 0
        && arguments[arguments.length - 1]
        && arguments[arguments.length - 1].hasOwnProperty("__kwargtrans__")
	) {
        var children = Array.prototype.slice.call(arguments, 0, -1);
        var props = arguments[arguments.length - 1];
    } else {
        var children = Array.prototype.slice.call(arguments, 0, -1);
        var props = {};
    }
    delete props.__kwargtrans__;
    return window.React.createElement(
        react_to_py,
        props,
        ...(Array.isArray(children) ? children : [children])
    );
}
""",
        globals={"react_to_py": react_to_py},
    )
    def py_to_react(*children, **props): ...

    return py_to_react


def component(fn: Callable):
    """
    Decorator to turn a Python function into a Pret component, that
    will be rendered by React.

    Parameters
    ----------
    fn: Callable
    """
    # When decorating a rendering function
    #
    # @component
    # def my_component(*children, **props):
    #     ...
    #
    # this will return a "wrapped" function.
    # Then, either the user calls it from Python, which will return a
    # Renderable object, it is called in the browser. In this case, it's
    # not the "wrapped" function that is called, but the transformed
    # "create_fn" function
    create_fn = make_create_element_from_function(fn)

    @functools.wraps(fn)
    @marshal_as(create_fn)
    def wrapped(*children, detach=False, **props):
        def render_x():
            return create_fn(*children, **props)

        return Renderable(
            render_x,
            detach=detach,
        )

    return wrapped


class ClientRef:
    registry = {}

    def __init__(self, id):
        self.id = id
        self.current = None
        ClientRef.registry[id] = self

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.__dict__["current"] = None
        ClientRef.registry[self.id] = self

    def __call__(self, element):
        self.current = element

    def __repr__(self):
        return f"Ref(id={self.id}, current={repr(self.current)})"


@marshal_as(ClientRef)
class Ref:
    registry = {}

    def __init__(self, id):
        self.id = id

    def _remote_call(self, attr, *args, **kwargs):
        return get_manager().remote_call(attr, args, kwargs)

    def __getattr__(self, attr):
        return functools.partial(self._remote_call, attr)


class Renderable:
    def __init__(self, obj, detach):
        self.obj = obj
        self.detach = detach
        self.marshaler = None
        self.data = None

    def ensure_marshaler(self):
        # Not in __init__ to allow a previous overwritten view
        # to be deleted and garbage collected
        if self.marshaler is None:
            import gc

            gc.collect()
            self.marshaler = get_shared_marshaler()
        return self.marshaler

    def __reduce__(self):
        return self.obj, ()

    def bundle(self):
        return self.ensure_marshaler().dump((self.obj, get_manager()))

    def _repr_mimebundle_(self, *args, **kwargs):
        plaintext = repr(self)
        if len(plaintext) > 110:
            plaintext = plaintext[:110] + "…"
        data, chunk_idx = self.bundle()
        return {
            "text/plain": plaintext,
            "application/vnd.pret+json": {
                "detach": self.detach,
                "version_major": 0,
                "version_minor": 0,
                "view_data": {
                    "marshaler_id": self.marshaler.id,
                    "serialized": data,
                    "chunk_idx": chunk_idx,
                },
            },
        }
