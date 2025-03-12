import os
import subprocess
from pathlib import Path

import typer

app = typer.Typer()


def get_current_pyproject_name():
    path = Path.cwd()
    while not (path / "pyproject.toml").exists():
        if path == path.parent:
            raise FileNotFoundError("pyproject.toml not found")
        path = path.parent
    # I don't want to add another dependency just for this
    text = (path / "pyproject.toml").read_text()
    project_section = text[text.index("[project]") : text.index("\n[")]
    name_idx = project_section.index("name")
    name = project_section[name_idx:].split("\n")[0].split("=")[1].strip().strip('"')
    return name


@app.command()
def stub(path_to_js_module: str, global_name: str, output_path: str):
    script_path = Path(__file__).parent / "generate-py-stubs.js"
    env = os.environ.copy()
    env["NODE_PATH"] = str(Path.cwd())
    pyproject_name = get_current_pyproject_name()
    # subprocess.run(["yarn", "add", "typescript@4.5.5", "--dev"], check=True)
    print(path_to_js_module, pyproject_name, global_name, output_path)
    subprocess.run(
        [
            "node",
            script_path,
            path_to_js_module,
            pyproject_name,
            global_name,
            output_path,
        ],
        env=env,
        check=True,
    )


if __name__ == "__main__":
    app()
