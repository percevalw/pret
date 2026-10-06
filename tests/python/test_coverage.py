"""
Check that coverage reports preserve original source lines and execution contexts
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pret.testing.sources import js_literals, read_source

coverage = pytest.importorskip("coverage", minversion="7.10")
report = pytest.importorskip("pret.testing.coverage").report


@pytest.mark.parametrize("runtime", ["node", "browser", "named"])
def test_original_sources_and_contexts(tmp_path, runtime):
    """
    Map Transcrypt and V8 execution to original sources while keeping untouched lines missing
    """
    root = Path(__file__).resolve().parents[2]
    fixture = root / "tests/coverage_fixture/sample.py"
    typed = fixture.with_suffix(".ts")
    untouched = tmp_path / "untouched.py"
    untouched.write_text("def untouched():\n    return 103\n")
    files = {}
    for path in [fixture, typed, untouched]:
        snapshot = read_source(str(path))
        lines, excluded = (
            coverage.Coverage(config_file=False).analysis2(str(path))[1:3] if path.suffix == ".py" else ([], [])
        )
        files[str(path)] = {
            **snapshot,
            "language": "python" if path.suffix == ".py" else "javascript",
            "lines": lines,
            "excluded": excluded,
            "javascript": js_literals(str(path)) if path.suffix == ".py" else [],
        }
    (tmp_path / "inventory.json").write_text(json.dumps({"roots": {"pret": str(root)}, "files": files, "skipped": []}))
    attempt = tmp_path / "attempts/000"
    (attempt / "python").mkdir(parents=True)
    (attempt / "attempt.json").write_text(
        json.dumps(
            {
                "suite": "fixture",
                "test": "fixture",
                "attempt": 0,
                "status": "passed",
            }
        )
    )
    env = {
        **os.environ,
        "PRET_COVERAGE_DIR": str(attempt),
        "PRET_COVERAGE_TEST": "fixture",
        "PYTHONPATH": os.pathsep.join([str(root), str(root / "vendor/transcrypt"), os.environ.get("PYTHONPATH", "")]),
    }
    env.pop("COVERAGE_PROCESS_START", None)
    env.pop("COVERAGE_RCFILE", None)
    env.pop("COVERAGE_PROCESS_CONFIG", None)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "coverage",
            "run",
            f"--data-file={attempt / 'python/.coverage'}",
            "--context=fixture",
            str(fixture),
            str(attempt / "bundle.json"),
        ],
        env=env,
        check=True,
    )
    helper = root / "pret/testing/playwright.cjs"
    config = tmp_path / "playwright.config.cjs"
    config.write_text(
        f"module.exports = {{...require({json.dumps(str(root / 'pret/testing/playwright.config.cjs'))}), "
        f"testDir: {json.dumps(str(fixture.parent))}, retries: 0}};\n"
    )
    # Each Playwright process records one case so failed execution keeps its own coverage status
    cases = ["positive", "negative", "failure"] if runtime == "named" else [runtime]
    for idx, case in enumerate(cases):
        case_attempt = tmp_path / "attempts" / f"{idx + 1:03d}"
        case_attempt.mkdir()
        test_id = f"fixture::{case}" if runtime == "named" else "fixture"
        result = subprocess.run(
            [
                str(root / "node_modules/.bin/playwright"),
                "test",
                "--config",
                str(config),
                "--project",
                "chromium",
                "--grep",
                f"{case}$",
                "--reporter",
                "line",
                "--output",
                str(case_attempt / "playwright"),
            ],
            cwd=root,
            env={
                **env,
                "PRET_TESTING_HELPER": str(helper),
                "PRET_FIXTURE_BUNDLE": str(attempt / "bundle.json"),
                "PRET_COVERAGE_DIR": str(case_attempt),
                "PRET_COVERAGE_TEST": test_id,
            },
        )
        assert result.returncode == (1 if case == "failure" else 0)
        (case_attempt / "attempt.json").write_text(
            json.dumps(
                {
                    "suite": "fixture",
                    "test": test_id,
                    "attempt": 0,
                    "status": "failed" if result.returncode else "passed",
                }
            )
        )
    report(tmp_path)
    output = json.loads((tmp_path / "coverage.json").read_text())
    python_report = next(file for name, file in output["files"].items() if name.endswith("sample.py"))
    untouched_report = next(file for name, file in output["files"].items() if name.endswith("untouched.py"))
    assert untouched_report["missing_lines"] == [1, 2]
    assert not untouched_report["executed_lines"]
    lines = fixture.read_text().splitlines()
    line = {text: idx + 1 for idx, text in enumerate(lines)}
    assert line["    return -999"] in python_report["missing_lines"]
    assert line["    return 101"] in python_report["missing_lines"]
    js_hit = line["        return 21;" if runtime == "browser" else "    return -21;"]
    js_miss = line["    return -21;" if runtime == "browser" else "        return 21;"]
    assert js_hit in python_report["executed_lines"]
    if runtime == "named":
        assert js_miss in python_report["executed_lines"]
        assert python_report["contexts"][str(js_hit)] == ["node|fixture::negative|attempt=0|status=passed"]
        assert python_report["contexts"][str(js_miss)] == ["node|fixture::positive|attempt=0|status=passed"]
        generated = next(file for name, file in output["files"].items() if name.endswith(".js"))
        assert generated["contexts"]["1"] == ["node|fixture::failure|attempt=0|status=failed"]
        report(tmp_path, passing_only=True)
        passing = json.loads((tmp_path / "coverage.json").read_text())
        assert not next(file for name, file in passing["files"].items() if name.endswith(".js"))["executed_lines"]
        report(tmp_path)
    else:
        assert js_miss in python_report["missing_lines"]
        assert all(context.startswith(runtime + "|") for context in python_report["contexts"][str(js_hit)])
    collection = json.loads((tmp_path / "collection.json").read_text())
    assert not collection["errors"]
    assert any(item["reason"] == "bytecode reconstructed source" for item in collection["excluded"])
    assert any(item["reason"] == "source without a file" for item in collection["excluded"])
    assert any(name.endswith(".js") for name in output["files"])
    if runtime == "browser":
        ts_report = next(file for name, file in output["files"].items() if name.endswith("sample.ts"))
        assert 4 in ts_report["executed_lines"]
        assert 10 in ts_report["missing_lines"]
    before = (tmp_path / "coverage.json").read_text()
    report(tmp_path)
    assert json.loads(before)["files"] == json.loads((tmp_path / "coverage.json").read_text())["files"]

    if runtime == "node":
        untouched.write_text("def untouched():\n    return 104\n")
        with pytest.raises(ValueError, match="Source changed"):
            report(tmp_path)
        untouched.write_text("def untouched():\n    return 103\n")
        (attempt / "python/missing.started").write_text(json.dumps({"data": str(attempt / "python/.coverage.missing")}))
        with pytest.raises(RuntimeError, match="Python process did not save coverage"):
            report(tmp_path)


def test_python_process_shutdown(tmp_path):
    """
    Check process contexts through normal exit, immediate exit and SIGTERM
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "shutdown_fixture"\n[tool.pret.testing]\npython = ["tests.py"]\n'
    )
    package = tmp_path / "shutdown_fixture"
    package.mkdir()
    (package / "process.py").write_text(
        "import os, signal, sys\n"
        "mode = sys.argv[1]\n"
        'print("ready", flush=True)\n'
        'if mode == "immediate":\n    os._exit(0)\n'
        'if mode == "signal":\n    signal.pause()\n'
    )
    (tmp_path / "tests.py").write_text(
        "import subprocess, sys\nimport pytest\n"
        '@pytest.mark.parametrize("mode", ["normal", "immediate", "signal"])\n'
        "def test_process(mode):\n"
        '    command = [sys.executable, "shutdown_fixture/process.py", mode]\n'
        "    child = subprocess.Popen(command, stdout=subprocess.PIPE)\n"
        '    assert child.stdout.readline() == b"ready\\n"\n'
        '    if mode == "signal":\n        child.terminate()\n'
        '    assert child.wait(timeout=10) == (-15 if mode == "signal" else 0)\n'
    )
    env = dict(os.environ)
    for name in ["COVERAGE_PROCESS_START", "COVERAGE_PROCESS_CONFIG", "COVERAGE_RCFILE"]:
        env.pop(name, None)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pret.testing",
            "run",
            "--coverage",
            "--suite",
            "python",
            "--output",
            "coverage",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    output = json.loads((tmp_path / "coverage/coverage.json").read_text())
    process = next(file for name, file in output["files"].items() if name.endswith("shutdown_fixture/process.py"))
    assert not process["missing_lines"]
    assert {context.split("test_process[", 1)[1].split("]", 1)[0] for context in process["contexts"]["3"]} == {
        "normal",
        "immediate",
        "signal",
    }
    assert len(list((tmp_path / "coverage/attempts/000/python").glob("*.started"))) == 3
    (tmp_path / "coverage").rename(tmp_path / "downloaded")
    report(tmp_path / "downloaded")
    downloaded = json.loads((tmp_path / "downloaded/coverage.json").read_text())
    # Coverage returns line contexts in set order
    for result in [output, downloaded]:
        for file in result["files"].values():
            file["contexts"] = {line: sorted(contexts) for line, contexts in file["contexts"].items()}
    assert downloaded["files"] == output["files"]
