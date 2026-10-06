"""
Run the same component tests with optional coverage and a fresh workspace for each Playwright case
"""

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


def stop(process):
    """
    Terminate the process group of a running test command or Jupyter server, kill it after 20 seconds and return
    whether it had to be killed
    """
    if process.poll() is not None:
        return False
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=20)
        return False
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        return True


def execute(command, cwd, env, log):
    """
    Save test output and stop the complete process group if the runner is interrupted
    """
    print("Running", " ".join(command), flush=True)
    with log.open("w") as output:
        child = subprocess.Popen(
            command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            return child.wait()
        finally:
            stop(child)


def playwright_cases(root, env, directory, suite, match, browsers):
    """
    Discover named cases so each invocation has separate server state and coverage attribution
    """
    cases = []
    executable = str(root / "node_modules/.bin/playwright")
    config = str(Path(__file__).with_name("playwright.config.cjs"))
    for browser in browsers:
        output = directory / f"{suite}-{browser}-list.json"
        command = [executable, "test", "--config", config, f"tests/{suite}", f"--project={browser}"]
        if match:
            command += ["--grep", match]
        if execute(
            [*command, "--list", "--reporter=json", "--pass-with-no-tests"],
            root,
            {**env, "PLAYWRIGHT_JSON_OUTPUT_FILE": str(output)},
            output.with_suffix(".log"),
        ):
            raise RuntimeError(f"Playwright discovery failed, see {output.with_suffix('.log')}")

        # Expand nested suites into commands selecting one exact Playwright title
        def visit(group, titles):
            # File groups are not part of the title expression used by Playwright grep
            for spec in group.get("specs", []):
                title = " ".join([*titles, spec["title"]])
                cases.append(
                    (
                        suite,
                        f"{suite}:{browser}:{spec['file']}::{title}",
                        [
                            executable,
                            "test",
                            "--config",
                            config,
                            spec["file"],
                            "--grep",
                            re.escape(title) + "$",
                            f"--project={browser}",
                            "--workers=1",
                            "--retries=0",
                            "--reporter=line",
                        ],
                    )
                )
            for child in group.get("suites", []):
                visit(child, [*titles, child["title"]] if child.get("column", 0) else titles)

        for group in json.loads(output.read_text())["suites"]:
            visit(group, [])
    return cases


def run_jupyter(command, root, roots, env, directory):
    """
    Start one JupyterLab server and stop its kernels before the next case uses a new workspace
    """
    import psutil

    data = directory / "jupyter-data"
    specifications = {"python3": ("Python 3", [sys.executable, "-m", "ipykernel_launcher"])}
    if "metanno" in roots:
        # Metanno tests start the explorer kernel through their tests/jupyter/explorer_kernel.py fixture
        specifications["metanno-explorer"] = (
            "Metanno Data Explorer",
            [sys.executable, str(Path(env["PRET_TEST_ROOT"]) / "explorer_kernel.py")],
        )
    for name, (display, argv) in specifications.items():
        kernel = data / "kernels" / name
        kernel.mkdir(parents=True)
        (kernel / "kernel.json").write_text(
            json.dumps(
                {
                    "argv": [*argv, "-f", "{connection_file}"],
                    "display_name": display,
                    "language": "python",
                }
            )
        )
    extensions = data / "labextensions"
    extensions.mkdir()
    for name, source in roots.items():
        (extensions / name).symlink_to(source / name / "js-extension", target_is_directory=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env = {
        **env,
        "PRET_TEST_URL": url,
        "PRET_TEST_DATA": str(data),
        "JUPYTER_DATA_DIR": str(data),
        "JUPYTER_CONFIG_DIR": str(directory / "jupyter-config"),
        "JUPYTER_CONFIG_PATH": "",
        "JUPYTER_RUNTIME_DIR": str(directory / "jupyter-runtime"),
        "JUPYTER_PATH": os.pathsep.join([str(data), str(Path(sys.prefix) / "share/jupyter")]),
    }
    with (directory / "server.log").open("w") as log:
        server = subprocess.Popen(
            [
                env.get("JUPYTER_PYTHON", sys.executable),
                "-m",
                "jupyterlab",
                "--config",
                str(Path(__file__).with_name("jupyter_config.py")),
                f"--ServerApp.port={port}",
            ],
            cwd=env["PRET_TEST_ROOT"],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 120
            while True:
                if server.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(f"Jupyter did not start, see {directory / 'server.log'}")
                try:
                    with urllib.request.urlopen(url + "/api", timeout=1):
                        break
                except (urllib.error.URLError, TimeoutError):
                    time.sleep(0.25)
            return execute(command, root, env, directory / "test.log")
        finally:
            try:
                # Jupyter interrupts kernel process groups during shutdown, let active compilers finish first
                deadline = time.monotonic() + 60
                while server.poll() is None:
                    try:
                        descendants = psutil.Process(server.pid).children(recursive=True)
                    except psutil.NoSuchProcess:
                        break
                    compilers = []
                    for process in descendants:
                        try:
                            if process.cmdline()[1:3] == ["-m", "transcrypt"]:
                                compilers.append(process)
                        except psutil.NoSuchProcess:
                            continue
                    if not compilers:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError("Transcrypt processes did not finish before Jupyter shutdown")
                    psutil.wait_procs(compilers, timeout=min(1, remaining))
                # Keep the server event loop running until its kernel shutdown requests have completed
                if server.poll() is None:
                    with urllib.request.urlopen(url + "/api/kernels", timeout=10) as response:
                        kernels = json.load(response)
                    for kernel in kernels:
                        request = urllib.request.Request(url + "/api/kernels/" + kernel["id"], method="DELETE")
                        with urllib.request.urlopen(request, timeout=60):
                            pass
            finally:
                if stop(server):
                    raise RuntimeError("Jupyter required forced shutdown")


def run(root, directory, suite, match, browsers, collect):
    """
    Execute tests for the command line and return its exit status

    1, Prepare isolated output and optional source inventory
    2, Discover configured Python and named Playwright cases
    3, Execute each case and save its attempt status
    4, Generate reports from saved coverage when requested
    """
    # 1, Prepare isolated output and optional source inventory
    project = tomllib.loads((root / "pyproject.toml").read_text())
    name = project["project"]["name"]
    pret = Path(__file__).resolve().parents[2]
    roots = {"pret": pret, name: root}
    if directory.exists():
        if not (directory / "run.json").is_file():
            raise ValueError(f"Refusing to replace a directory without test run metadata, {directory}")
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    (directory / "attempts").mkdir()
    (directory / "run.json").write_text(json.dumps({"project": name, "suite": suite, "coverage": collect}))
    env = {
        **os.environ,
        "PYTHON": sys.executable,
        "PRET_TESTING_HELPER": str(Path(__file__).with_name("playwright.cjs")),
        "PYTHONPATH": os.pathsep.join(
            [str(root), str(pret), str(pret / "vendor/transcrypt"), os.environ.get("PYTHONPATH", "")]
        ),
    }
    if collect:
        from .coverage import inventory

        (directory / "inventory.json").write_text(json.dumps(inventory(roots)))
        startup = directory / "startup"
        startup.mkdir()
        shutil.copyfile(Path(__file__).with_name("startup.py"), startup / "sitecustomize.py")
        env.update(PRET_COVERAGE="1", PYTHONPATH=str(startup) + os.pathsep + env["PYTHONPATH"])
    else:
        for key in [
            "PRET_COVERAGE",
            "PRET_COVERAGE_DIR",
            "PRET_COVERAGE_TEST",
            "COVERAGE_PROCESS_START",
            "COVERAGE_PROCESS_CONFIG",
            "COVERAGE_RCFILE",
            "COVERAGE_FILE",
        ]:
            env.pop(key, None)
    # 2, Discover configured Python and named Playwright cases
    cases = []
    if suite in {"all", "python"}:
        command = [sys.executable, "-m", "pytest", *project["tool"]["pret"]["testing"]["python"]]
        if match:
            command += ["-k", match]
        cases.append(("pytest", f"pytest:{name}", command))
    if suite in {"all", "client"}:
        cases += playwright_cases(root, env, directory, "client", match, browsers)
    if suite in {"all", "jupyter"}:
        env["JUPYTERLAB_VERSION"] = subprocess.check_output(
            [
                env.get("JUPYTER_PYTHON", sys.executable),
                "-c",
                "import jupyterlab; print(jupyterlab.__version__)",
            ],
            text=True,
        ).strip()
        cases += playwright_cases(root, env, directory, "jupyter", match, browsers)
    if not cases:
        raise ValueError("No tests matched the selected suite")
    # 3, Execute each case and save its attempt status
    failed = False
    executed = False
    for idx, (kind, test, command) in enumerate(cases):
        attempt = directory / "attempts" / f"{idx:03d}"
        (attempt / "python").mkdir(parents=True)
        metadata = {"suite": kind, "test": test, "status": "incomplete", "command": command}
        (attempt / "attempt.json").write_text(json.dumps(metadata))
        workspace = attempt / "workspace"
        if kind == "jupyter":
            shutil.copytree(
                root / "tests/jupyter", workspace, ignore=shutil.ignore_patterns("__pycache__", "*.spec.ts")
            )
            # Retained examples such as Quaero run in the interpreter selected for this suite
            for notebook in workspace.rglob("*.ipynb"):
                content = json.loads(notebook.read_text())
                content["metadata"]["kernelspec"] = {
                    "name": "python3",
                    "display_name": "Python 3",
                    "language": "python",
                }
                notebook.write_text(json.dumps(content))
        else:
            workspace.mkdir()
        child_env = {**env, "PRET_TEST_ROOT": str(workspace)}
        if collect:
            config = attempt / "coverage.toml"
            config.write_text(
                "[tool.coverage.run]\nbranch = false\nparallel = true\nsigterm = true\n"
                'patch = ["_exit", "subprocess"]\n'
                f"context = {json.dumps(test)}\ndata_file = {json.dumps(str(attempt / 'python/.coverage'))}\n"
                f"source = {json.dumps([str(value / key) for key, value in roots.items()])}\n"
                'omit = ["*/pret/testing/*"]\n'
            )
            child_env.update(
                PRET_COVERAGE_DIR=str(attempt),
                PRET_COVERAGE_TEST=test,
                COVERAGE_PROCESS_START=str(config),
                COVERAGE_RCFILE=str(config),
                COVERAGE_FILE=str(attempt / "python/.coverage"),
            )
            if kind == "pytest":
                child_env.pop("COVERAGE_PROCESS_START")
                child_env.pop("COVERAGE_PROCESS_CONFIG", None)
                command += [
                    "-p",
                    "sitecustomize",
                    "--cov",
                    "--cov-context=test",
                    "--cov-report=",
                    f"--cov-config={config}",
                ]
        if kind != "pytest":
            command += ["--output", str(attempt / "playwright")]
        started = time.monotonic()
        try:
            code = (
                run_jupyter(command, root, roots, child_env, attempt)
                if kind == "jupyter"
                else execute(command, root, child_env, attempt / "test.log")
            )
            # A name filter can select another group without matching any Python cases
            metadata["status"] = (
                "skipped" if kind == "pytest" and match and code == 5 else ("passed" if code == 0 else "failed")
            )
            failed |= metadata["status"] == "failed"
            executed |= metadata["status"] != "skipped"
        except Exception as error:
            failed = True
            executed = True
            metadata["status"] = "failed"
            with (attempt / "errors.jsonl").open("a") as output:
                output.write(json.dumps({"error": str(error)}) + "\n")
            # Failed infrastructure cleanup must stop the run before another server starts
            break
        finally:
            metadata["seconds"] = round(time.monotonic() - started, 2)
            (attempt / "attempt.json").write_text(json.dumps(metadata))
            print(f"{metadata['status']}, {test}, {attempt / 'test.log'}", flush=True)
            if metadata["status"] in {"passed", "skipped"}:
                shutil.rmtree(workspace)
    if not executed:
        raise ValueError("No tests matched the selected suite")
    # 4, Generate reports from saved coverage when requested
    if collect:
        from .coverage import report

        report(directory)
    return int(failed)
