import sys
from typing import Any, Union

from pret.marshal import js, make_stub_js_module, marshal_as
from pret.render import stub_component

__version__ = "0.4.2"
_py_package_name = "pret"
_js_package_name = "pret"
_js_global_name = "PretRouter"

make_stub_js_module("PretRouter", "pret", "pret", __version__, __name__)

if sys.version_info >= (3, 8):
    pass
else:
    pass

props_mapping = {
    "case_sensitive": "caseSensitive",
    "component": "Component",
    "error_boundary": "ErrorBoundary",
    "error_element": "errorElement",
    "has_error_boundary": "hasErrorBoundary",
    "hydrate_fallback": "HydrateFallback",
    "hydrate_fallback_element": "hydrateFallbackElement",
    "should_revalidate": "shouldRevalidate",
    "intercept_anchor_clicks": "interceptAnchorClicks",
    "default_init": "defaultInit",
}


@stub_component(js.PretRouter.Routes, props_mapping)
def Routes(*children, key: Union[str, int], location: Any):
    """"""


@stub_component(js.PretRouter.Route, props_mapping)
def Route(
    *children,
    action: Any,
    case_sensitive: Any,
    component: Any,
    element: Any,
    error_boundary: Any,
    error_element: Any,
    handle: Any,
    has_error_boundary: Any,
    hydrate_fallback: Any,
    hydrate_fallback_element: Any,
    id: Any,
    index: Any,
    key: Union[str, int],
    lazy: Any,
    loader: Any,
    path: Any,
    should_revalidate: Any,
):
    """"""


@stub_component(js.PretRouter.BrowserRouter, props_mapping)
def BrowserRouter(
    *children,
    basename: str,
    future: Any,
    intercept_anchor_clicks: Union[Any, bool],
    key: Union[str, int],
    window: Any,
):
    """"""


@stub_component(js.PretRouter.HashRouter, props_mapping)
def HashRouter(
    *children,
    basename: str,
    future: Any,
    intercept_anchor_clicks: Union[Any, bool],
    key: Union[str, int],
    window: Any,
):
    """"""


@stub_component(js.PretRouter.Outlet, props_mapping)
def Outlet(*children, context: Any, key: Union[str, int]):
    """"""


@marshal_as(js.PretRouter.useLocation)
def use_location():
    """"""


@marshal_as(js.PretRouter.useMatch)
def use_match(pattern: Any = None):
    """"""


@marshal_as(js.PretRouter.useParams)
def use_params():
    """"""


@marshal_as(js.PretRouter.useSearchParams)
def use_search_params(default_init: Any = None):
    """"""
