import importlib
from importlib.metadata import PackagePath
from types import SimpleNamespace

from typer.testing import CliRunner

from pret.cli import app

# pret.cli exports the command function under the name of its module
command = importlib.import_module("pret.cli.update_jupyter_config")


def test_update_jupyter_config_links_pret_extensions(tmp_path, monkeypatch):
    """
    Link the extensions of pret and of packages requiring it into the user Jupyter data folder, leave the others
    """
    prefix = tmp_path / "env"
    labextensions = prefix / "share" / "jupyter" / "labextensions"
    for name in ("pret", "metanno", "@jupyter-widgets/jupyterlab-manager"):
        (labextensions / name).mkdir(parents=True)
        (labextensions / name / "package.json").write_text("{}")

    def dist(name, extension, requires=()):
        return SimpleNamespace(
            metadata={"Name": name},
            requires=list(requires),
            files=[PackagePath(f"../../../share/jupyter/labextensions/{extension}/package.json")],
            locate_file=lambda file: labextensions / "/".join(file.parts[file.parts.index("labextensions") + 1 :]),
        )

    monkeypatch.setattr(
        command,
        "distributions",
        lambda: [
            dist("pret", "pret"),
            dist("metanno", "metanno", ["pret>=0.7.0b1", "ipykernel>=6"]),
            dist("pret-joy-like", "@jupyter-widgets/jupyterlab-manager", ["pret-joy"]),
        ],
    )
    data = tmp_path / "data"
    monkeypatch.setenv("JUPYTER_DATA_DIR", str(data))
    runner = CliRunner()

    # Without --apply the command only prints the links
    result = runner.invoke(app, ["update-jupyter-config"])
    assert result.exit_code == 0 and "Would link" in result.output and not data.exists()

    # A link left by another environment is replaced, packages not requiring pret are not linked
    (data / "labextensions").mkdir(parents=True)
    (data / "labextensions" / "metanno").symlink_to(tmp_path / "removed-env", target_is_directory=True)
    result = runner.invoke(app, ["update-jupyter-config", "--apply"])
    assert result.exit_code == 0, result.output
    assert sorted(path.name for path in (data / "labextensions").iterdir()) == ["metanno", "pret"]
    for name in ("pret", "metanno"):
        assert (data / "labextensions" / name).resolve() == (labextensions / name).resolve()
    assert "Linked" in result.output and "Refresh JupyterLab" in result.output
