#!/usr/bin/env python3
import argparse
import ast
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import typer

app = typer.Typer()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Update Jupyter's extra_labextensions_path for the current user."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Don't write files; only show what would change (default).",
    )
    group.add_argument(
        "--apply",
        dest="dry_run",
        action="store_false",
        help="Write changes to disk.",
    )
    return parser.parse_args()


def find_home_config_dir() -> Path:
    """
    Choose a config directory that lives in the user's HOME
    (e.g. ~/.jupyter), not inside the active conda environment.
    """
    try:
        from jupyter_core.paths import jupyter_config_path

        cfg_paths = [Path(p).resolve() for p in jupyter_config_path()]
    except Exception:
        # Fallback: prefer ~/.jupyter
        cfg_paths = [Path.home() / ".jupyter"]

    home = Path.home().resolve()
    env_prefix = Path(sys.prefix).resolve()

    # Prefer the first path that is under HOME and not under the env prefix
    for p in cfg_paths:
        print(f"Considering jupyter config file path: {p}")
    for p in cfg_paths:
        if str(p).startswith(str(home)) and not str(p).startswith(str(env_prefix)):
            print(f"Selected jupyter config file path: {p} ✅")
            return p

    # Otherwise default to ~/.jupyter
    return home / ".jupyter"


def env_labextensions_dir() -> Path:
    """
    Compute <env>/share/jupyter/labextensions for the active environment.
    """
    env_prefix = Path(sys.prefix).resolve()
    return env_prefix / "share" / "jupyter" / "labextensions"


def ensure_dir(path: Path, dry_run: bool = True) -> None:
    if not dry_run:
        path.mkdir(parents=True, exist_ok=True)


def set_extra_labextensions_path(
    cfg_file: Path, labext_dir: str, dry_run: bool = True
) -> Tuple[str, str, str]:
    """
    Create or update jupyter_server_config.py to set:
      c.LabServerApp.extra_labextensions_path = ["<labext_dir>", ...]
    If a previous assignment exists, replace it, otherwise append.
    Returns a tuple of (old_line, new_line, backup_path) where backup_path is the path
    to the backup file if created.
    """

    content = ""
    if cfg_file.exists():
        content = cfg_file.read_text(encoding="utf-8")

    var_name = "c.LabServerApp.extra_labextensions_path"

    # Replace/update any existing assignment for extra_labextensions_path
    pattern = re.compile(
        rf"^\s*{re.escape(var_name)}\s*=\s*\[(.*?)\]\s*$",
        flags=re.MULTILINE,
    )

    old_line = None
    old_line_match = pattern.search(content)
    if old_line_match:
        old_line = old_line_match.group()
        old_values = ast.literal_eval(old_line_match.group(1))
        if isinstance(old_values, str):
            old_values = [old_values]
        old_values = list(old_values)
        if labext_dir in old_values:
            print(f"⚠️ Warning: {labext_dir} was already in extra_labextensions_path")
            old_values = [x for x in old_values if x != labext_dir]
        old_values = ", ".join(f'"{v}"' for v in old_values)
        new_line = f'{var_name} = ["{str(labext_dir)}", {old_values}]\n'
    else:
        new_line = f'{var_name} = ["{str(labext_dir)}"]\n'

    if old_line:
        content = pattern.sub(new_line.rstrip(), content)
    else:
        if content and not content.endswith("\n"):
            content += "\n"
        content += new_line

    backup_path = ""

    if not dry_run:
        # Backup the existing file (if any) before writing changes
        if cfg_file.exists():
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = cfg_file.with_name(cfg_file.name + f".bak-{ts}")
            shutil.copy2(str(cfg_file), str(backup_path))
        cfg_file.write_text(content, encoding="utf-8")

    return (
        old_line.strip() if old_line else None,
        pattern.search(content).group().strip(),
        str(backup_path) if backup_path else "",
    )


@app.command()
def update_jupyter_config(
    dry_run: Optional[bool] = None,
    apply: bool = False,
):
    if dry_run is None:
        dry_run = not apply
    else:
        dry_run = bool(dry_run)

    if dry_run:
        print(
            "⚠️ Running in dry-run mode. No changes will be written to disk. "
            "You can use --apply to write changes."
        )
    else:
        print("ℹ️ Running in apply mode. Changes will be written to disk.")
    print()

    try:
        import pret  # noqa: F401
    except ImportError:
        print("❌ It seems that you are not in an environment with pret installed")
        print("Is this Python executable the one of your environment ?")
        print("  ", sys.executable)
        sys.exit(1)

    cfg_dir = find_home_config_dir()
    ensure_dir(cfg_dir, dry_run=dry_run)

    cfg_file = cfg_dir / "jupyter_config.py"
    labext = env_labextensions_dir()
    ensure_dir(labext, dry_run=dry_run)

    old_line, new_line, backup_path = set_extra_labextensions_path(
        cfg_file, labext, dry_run=dry_run
    )

    print("✅ Done.")
    print(f"Config dir:   {cfg_dir}")
    print(f"Config file:  {cfg_file}")
    print(f"Labext dir:   added '{labext}'")
    if not dry_run:
        if backup_path:
            print(f"Backup file:  {backup_path}")
        else:
            print("Backup file:  (no previous config found)")
    print(f"  OLD: {old_line or 'No previous extra_labextensions_path value'}")
    print(f"  NEW: {new_line}")
    print("You can now:")
    print("  1) Restart your Jupyter server (File → Control Panel → Stop → Start).")
    print("  2) Refresh your browser tab.")
    print("  3) Verify with `jupyter labextension list` run in the base environment.")


if __name__ == "__main__":
    app()
