import os
import sys
from pathlib import Path

import typer

sys.path.insert(0, os.getcwd())

from pret.main import extract_js_dependencies
from pret.marshal import get_shared_marshaler

app = typer.Typer()


@app.command()
def prepack(stub_module: str, output_path: str, cwd: bool = True):
    """
    Pre-packs a stub module into a single javascript file.

    Parameters
    ----------
    stub_module
        The name of the stub module to prepack.
    output_path
        The path to the output file.
    cwd
        Whether to add the current working directory to the python path.
    """

    output_path = Path(output_path)

    if cwd:
        sys.path.insert(0, os.getcwd())

    module = __import__(stub_module, fromlist=["*"])
    print(f"Pre-pack from {stub_module} at {module.__file__}")

    pickler = get_shared_marshaler()
    pickler.dump(module)
    js_globals_file_str = extract_js_dependencies(pickler.accessed_global_refs)[0]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as f:
        f.write(js_globals_file_str)


if __name__ == "__main__":
    app()
