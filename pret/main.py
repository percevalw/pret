import contextlib
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union
import tempfile

from pret.serialize import get_shared_pickler
from pret.serve import make_app


class RawRepr(str):
    def __repr__(self):
        return self


class BundleMode(str, Enum):
    FEDERATED = "federated"


@contextlib.contextmanager
def build(
    renderables,
    static_dir: Union[str, Path] = None,
    build_dir: Union[str, Path] = None,
    mode: Union[bool, str, BundleMode] = True,
) -> Dict[str, Union[str, Path]]:
    if mode != BundleMode.FEDERATED:
        raise Exception("Only supported mode is 'federated'")

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

    pickler = get_shared_pickler()
    pickle_file_str = ""
    for renderable in renderables:
        pickle_file_str = renderable.bundle()[0]

    # Extract js globals and them to a temp file to be bundled with webpack
    js_globals, packages = extract_js_dependencies(pickler.accessed_global_refs)

    content_hash = hashlib.md5(pickle_file_str.encode("utf-8")).hexdigest()[:20]
    pickle_filename = f"bundle.{content_hash}.pkl"

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
            .replace("__PRET_PICKLE_FILE__", pickle_filename)
        )

        assets = {
            **base_static_mapping,
            **extension_static_mapping,
            pickle_filename: pickle_file_str,
            "index.html": index_html_str,
        }

    yield assets

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
    refs: List[pret.serialize.GlobalRef]
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
        packages.append(ref.module._package_version.split(".")[0])

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
        stub_root = Path(sys.modules[package].__file__).parent
        static_dir = stub_root / "js-extension" / "static"
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

    for static_file in static_dir.glob("*"):
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
):
    with (
        build(
            [renderable],
            static_dir=static_dir,
            build_dir=build_dir,
            mode=bundle,
        )
        if bundle
        else contextlib.nullcontext({"*": Path(static_dir)})
    ) as assets:
        if serve:
            app = make_app(assets)
            app.run(debug=dev, port=5001)
