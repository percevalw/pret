import os
import subprocess
from pathlib import Path

import typer

app = typer.Typer()


@app.command()
def stub(package_name: str, exposed_name: str, output_path: str):
    script_path = Path(__file__).parent / "generate-py-stubs.js"
    env = os.environ.copy()
    env['NODE_PATH'] = str(Path.cwd())
    subprocess.run(['yarn', "add", "typescript@4.4.4", "--dev"], check=True)
    subprocess.run(['node', script_path, package_name, exposed_name, output_path], env=env, check=True)


if __name__ == "__main__":
    app()
