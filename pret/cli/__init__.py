import typer

from .stub_command import stub

app = typer.Typer()
app.command(name="stub")(stub)


@app.callback()
def callback():
    pass


if __name__ == "__main__":
    app()
