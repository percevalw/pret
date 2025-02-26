import typer

from .stub_command import stub
from .prepack_command import prepack

app = typer.Typer()
app.command(name="stub")(stub)
app.command(name="prepack")(prepack)


@app.callback()
def callback():
    pass


if __name__ == "__main__":
    app()
