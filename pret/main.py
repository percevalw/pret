import asyncio
import contextlib
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from pret.marshal import get_shared_marshaler
from pret.serve import make_app


class RawRepr(str):
    def __repr__(self):
        return self


class BundleMode(str, Enum):
    FEDERATED = "federated"
    MONOLITHIC = "monolithic"


@contextlib.contextmanager
def build(
    renderables,
    static_dir: Union[str, Path] = None,
    build_dir: Union[str, Path] = None,
    mode: Union[bool, str, BundleMode] = True,
    dev: bool = False,
) -> Tuple[Dict[str, Union[str, Path]], List[Tuple[str, str]], str]:
    """
    Build the Pret app, pooling all the assets and entry points from
    the packages that were accessed for rendering.

    Parameters
    ----------
    renderables: List[pret.render.Renderable]
        The list of renderables to be bundled
    static_dir: Union[str, Path]
        The directory where the static files will be stored
    build_dir: Union[str, Path]
        The directory where the build files will be stored
    mode: Union[bool, str, BundleMode]
        The mode to use for bundling :

        - "federated": The app will be bundled where the assets are
          just copied from each package. This means that there may
          be repeated assets in the final bundle.
        - "monolithic": The app will be bundled where a consolidated
          bundle is created with all the assets, using webpack.

        By default, the mode is "federated".
    dev: bool
        Only used when mode is "monolithic". If True, the bundle will
        be created in development mode. Otherwise, it will be created
        in production mode.

    Returns
    -------
    Tuple[Dict[str, Union[str, Path]], List[Tuple[str, str]], str]
        A tuple containing the assets, entries and pickle filename
    """
    npm = None
    if mode is True:
        mode = BundleMode.FEDERATED
        # npm = shutil.which("npm")
        # if not npm:
        #     if mode == BundleMode.MONOLITHIC:
        #         raise Exception("npm not found. Please install node to proceed.")
        #     else:
        #         mode = BundleMode.FEDERATED
        # elif mode != BundleMode.MONOLITHIC:
        #     mode = BundleMode.MONOLITHIC

    if static_dir is None:
        static_dir = Path(tempfile.mkdtemp())
        delete_static = True
    else:
        static_dir = Path(static_dir)
        delete_static = False

    if build_dir is None:
        build_dir = Path(tempfile.mkdtemp())
        delete_build = True
    else:
        build_dir = Path(build_dir)
        delete_build = False

    marshaler = get_shared_marshaler()
    marshaler_file_str = ""
    for renderable in renderables:
        marshaler_file_str = json.dumps(renderable.bundle()[0])

    # Extract js globals and them to a temp file to be bundled with webpack
    js_globals, packages = extract_js_dependencies(marshaler.accessed_global_refs)

    js_globals_file = build_dir / "globals.ts"
    content_hash = hashlib.md5(marshaler_file_str.encode("utf-8")).hexdigest()[:20]
    bundle_filename = f"bundle.{content_hash}.pkl"

    if mode == BundleMode.MONOLITHIC:
        with js_globals_file.open("w") as f:
            f.write(js_globals)

        with (static_dir / bundle_filename).open("w") as f:
            f.write(marshaler_file_str)

        webpack_config = Path(__file__).parent / "webpack.standalone.js"
        # fmt: off
        # run npm webpack ... in cwd:
        subprocess.check_call(
            [
                npm,
                "exec",
                "webpack",
                "--config", webpack_config,
                "--env", "pretGlobalsFile=" + str(js_globals_file),
                "--mode", "development" if dev else "production",
            ],
            cwd=os.getcwd(),
        )
        # fmt: on

        assets = {
            "*": static_dir,
        }

    entries: List[Tuple[str, Optional[str]]] = []
    if mode == BundleMode.FEDERATED:
        extension_static_mapping, entries = extract_prebuilt_extension_assets(packages)
        base_static_mapping, index_html = extract_prebuilt_base_assets()
        index_html_str = index_html.read_text()
        index_html_str = (
            index_html_str.replace(
                "/* PRET_HEAD_TAGS */",
                "".join(
                    '<script src="{}"></script>'.format(remote_entry_file)
                    for remote_entry_file, _ in entries
                ),
            )
            .replace(
                "'__PRET_REMOTE_IMPORTS__'",
                str([remote_name for _, remote_name in entries]),
            )
            .replace("__PRET_PICKLE_FILE__", bundle_filename)
        )

        assets = {
            **base_static_mapping,
            **extension_static_mapping,
            bundle_filename: marshaler_file_str,
            # override the index.html file in base_static_mapping
            "index.html": index_html_str,
        }

        # static_dir.mkdir(parents=True, exist_ok=True)
        # Include entry points in the index html file

    yield assets, entries, bundle_filename

    if delete_static:
        shutil.rmtree(static_dir)
    if delete_build:
        shutil.rmtree(build_dir)


def extract_js_dependencies(
    refs,
    exclude=(
        "js.React.*",
        "js.ReactDOM.*",
    ),
):
    """
    Create a js file that will import all the globals that were accessed during
    pickling and assign them to the global scope.

    Parameters
    ----------
    refs: List[pret.marshal.GlobalRef]
        List of Ref objects that were accessed during pickling
    exclude: List[str]
        List of module patterns to exclude from the js globals file

    Returns
    -------
    """

    exported = {}
    imports = defaultdict(list)
    packages = []

    for ref_idx, ref in enumerate(refs):
        if not ref.__module__.startswith("js.") or any(
            fnmatch.fnmatch(str(ref), x) for x in exclude
        ):
            continue

        js_module_path_parts = ref.name.split(".")
        packages.append(ref.module)

        imports[
            ".".join((ref.module._package_name, *js_module_path_parts[:-1]))
        ].append((js_module_path_parts[-1], f"i_{ref_idx}"))

        current = exported
        for part in (ref.__module__[3:], *js_module_path_parts[:-1]):
            if part not in current:
                current[part] = {}

            current = current[part]
        current[ref.name.split(".")[-1]] = RawRepr(f"i_{ref_idx}")

    js_file_string = ""

    for package, aliases in imports.items():
        aliases_str = ", ".join([obj + " as " + alias for obj, alias in aliases])
        js_file_string += f"import {{ {aliases_str} }} from '{package}';\n"

    js_file_string += "\n\n"

    for globalName, value in exported.items():
        js_file_string += f"(window as any).{globalName} = {repr(value)};\n"

    return js_file_string, packages


def extract_prebuilt_extension_assets(
    packages: List[str],
) -> Tuple[Dict[str, Path], List[Tuple[str, str]]]:
    """
    Extracts entry javascript files from the static directory of each package
    as well as a mapping entry -> file to know where to look for whenever the app
    asks for a chunk or an asset.

    Parameters
    ----------
    packages

    Returns
    -------
    Tuple[Dict[str, Path], List[Tuple[str, str]]]
    """
    mapping = {}
    entries = []
    for package in set(packages):
        try:
            # in case it's an editable install
            stub_root = Path(sys.modules[package._stub_qualified_name].__file__).parent
            static_dir = stub_root / "js-extension" / "static"
            entry = next(static_dir.glob("remoteEntry.*.js"))
        except StopIteration:
            # otherwise, it's a regular install
            js_package = package._js_package_name
            static_dir = (
                Path(sys.prefix) / f"share/jupyter/labextensions/{js_package}/static"
            )
            entry = next(static_dir.glob("remoteEntry.*.js"))
        remote_name = json.loads((static_dir.parent / "package.json").read_text())[
            "name"
        ]
        mapping[entry.name] = entry
        entries.append((entry.name, remote_name))

        for static_file in static_dir.glob("*"):
            if static_file.name not in mapping:
                mapping[static_file.name] = static_file

    return mapping, entries


def extract_prebuilt_base_assets() -> Tuple[Dict[str, Path], Path]:
    """
    Extracts the base index.html file as well as a mapping entry -> file to know where
    to look for whenever the app asks for a chunk or an asset.

    Parameters
    ----------

    Returns
    -------
    Tuple[Dict[str, Path], List[str]]
    """
    mapping = {}
    static_dir = Path(__file__).parent / "js-base"
    entry = next(static_dir.glob("index.html"))

    for static_file in static_dir.glob("*.js"):
        if static_file.name not in mapping:
            mapping[static_file.name] = static_file

    return mapping, entry


def run(
    renderable,
    static_dir: Optional[Union[str, Path]] = None,
    build_dir: Optional[Union[str, Path]] = None,
    bundle: Union[bool, str, BundleMode] = True,
    dev: bool = True,
    serve: bool = True,
    port: int = 5000,
):
    """
    Serve the app, after building the app if necessary.

    Parameters
    ----------
    renderable: pret.render.Renderable
        The renderable object to be served
    static_dir: Optional[Union[str, Path]]
        The directory where the static files will be stored
    build_dir: Optional[Union[str, Path]]
        The directory where the build files will be stored
    bundle: Union[bool, str, BundleMode]
        The mode to use for bundling :

        - "federated": The app will be bundled where the assets are
          just copied from each package. This means that there may
          be repeated assets in the final bundle.
        - "monolithic": The app will be bundled where a consolidated
          bundle is created with all the assets, using webpack.

        By default, the mode is "federated".
    dev: bool
        Only used when mode is "monolithic". If True, the bundle will
        be created in development mode. Otherwise, it will be created
        in production mode.
    serve: bool
        Whether to serve the app after building it.
    port: int
        The port to use for serving the app.
    """

    with (
        build(
            [renderable],
            static_dir=static_dir,
            build_dir=build_dir,
            mode=bundle,
        )
        if bundle
        else contextlib.nullcontext({"*": Path(static_dir)})
    ) as (assets, entries, bundle_filename):
        app = make_app(assets)
        if serve:
            app.run(debug=dev, port=port, loop=asyncio.get_event_loop())
        return app
