import typer

from .update_jupyter_config import update_jupyter_config
from .stub_command import stub
from .prepack_command import prepack

app = typer.Typer()
app.command(name="stub")(stub)
app.command(name="prepack")(prepack)
app.command(name="update-jupyter-config")(update_jupyter_config)


@app.callback()
def callback():
    pass


if __name__ == "__main__":
    app()
