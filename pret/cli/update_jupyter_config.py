"""
Make JupyterLab load the Pret extensions of a Python environment it does not run in
"""

import re
import sys
from importlib.metadata import distributions
from pathlib import Path

import typer


def pret_labextensions():
    """
    Return the JupyterLab extension folders installed in this environment by pret and by the packages requiring
    it, eg metanno and pret-joy, other extensions of the environment such as widgets are left to JupyterLab
    """
    folders = {}
    for dist in distributions():
        requires_pret = any(re.match(r"pret\b(?![-_.])", requirement) for requirement in dist.requires or ())
        if dist.metadata["Name"] != "pret" and not requires_pret:
            continue
        for file in dist.files or ():
            parts = file.parts
            if parts[-1] == "package.json" and "labextensions" in parts:
                # Scoped packages, eg @org/name, are one folder deeper than the labextensions folder
                name = "/".join(parts[parts.index("labextensions") + 1 : -1])
                folders[name] = Path(dist.locate_file(file)).parent.resolve()
    return folders


def update_jupyter_config(
    apply: bool = typer.Option(False, "--apply", help="Create the links, without it only print them"),
):
    """
    Link the Pret extensions of this environment into the user Jupyter data folder, eg
    ~/.local/share/jupyter/labextensions, which JupyterLab reads on each page load whatever its own environment.
    A browser refresh then loads them, the Jupyter server keeps running
    """
    from jupyter_core.paths import jupyter_data_dir

    target = Path(jupyter_data_dir()) / "labextensions"
    extensions = pret_labextensions()
    if not extensions:
        print(f"No Pret extension is installed in the environment of {sys.executable}")
        raise typer.Exit(1)
    for name, folder in sorted(extensions.items()):
        link = target / name
        if link.is_symlink() and link.resolve() == folder:
            print(f"Already linked: {link} -> {folder}")
        elif link == folder:
            print(f"Already in the user folder: {link}")
        elif link.exists() and not link.is_symlink():
            # A copy installed in the user folder, eg by pip install --user, is never removed by this command
            print(f"Kept: {link} is an installed copy that JupyterLab loads instead, remove it to use {folder}")
        else:
            print(f"{'Linked' if apply else 'Would link'}: {link} -> {folder}")
            if apply:
                link.parent.mkdir(parents=True, exist_ok=True)
                # A link to another environment, or to a removed one, points to this environment instead
                if link.is_symlink():
                    link.unlink()
                link.symlink_to(folder, target_is_directory=True)
    if not apply:
        print("Nothing was changed, run again with --apply to create the links")
    else:
        print("Refresh JupyterLab in your browser to load them")
