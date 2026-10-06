"""
Import Python and V8 line coverage into reports of original sources
"""

import hashlib
import json
import subprocess
from pathlib import Path

from coverage import Coverage, CoverageData, CoveragePlugin, FileReporter

from .sources import js_literals, read_source


class SourceReporter(FileReporter):
    """
    Display original mixed language files using the converter executable line inventory
    """

    def __init__(self, filename, metadata):
        super().__init__(filename)
        self.metadata = metadata

    def lines(self):
        return set(self.metadata["lines"])

    def excluded_lines(self):
        return set(self.metadata["excluded"])

    def source(self):
        return self.metadata["source"]

    def relative_filename(self):
        return self.metadata.get("display_path", self.filename)


class SourcePlugin(CoveragePlugin):
    """
    Supply original source and statement lines to coverage HTML and JSON reporting
    """

    def __init__(self, files):
        self.files = files

    def file_reporter(self, filename):
        return SourceReporter(filename, self.files[filename])


def coverage_init(reg, options):
    """
    Load saved inventory when coverage requests the reporting plugin
    """
    reg.add_file_tracer(SourcePlugin(json.loads(Path(options["inventory"]).read_text())["files"]))


def inventory(roots):
    """
    Inventory project sources before testing so untouched files remain visible in every report

    Sources left out of the report on purpose are listed in `skipped`, convert.cjs accepts tests that only run them
    """
    files = {}
    skipped = []
    analyzer = Coverage(config_file=False)
    for name, root in roots.items():
        for base in [root / name.replace("-", "_"), root / "client"]:
            for path in sorted(base.rglob("*")):
                if not path.is_file() or path.suffix not in {
                    ".py",
                    ".js",
                    ".jsx",
                    ".ts",
                    ".tsx",
                    ".mjs",
                    ".cjs",
                }:
                    continue
                if any(
                    part in {"testing", "node_modules", "__pycache__", "js-extension", "js-base"} for part in path.parts
                ):
                    continue
                if path.name.endswith(".d.ts"):
                    continue
                if path.name == "org.transcrypt.__runtime__.js":
                    skipped.append(str(path))
                    continue
                snapshot = read_source(str(path))
                if path.suffix == ".py":
                    _, lines, excluded, _, _ = analyzer.analysis2(str(path))
                    fragments = js_literals(str(path))
                else:
                    lines, excluded, fragments = [], [], []
                files[snapshot["path"]] = {
                    **snapshot,
                    "display_path": f"{name}/{path.relative_to(root)}",
                    "language": "python" if path.suffix == ".py" else "javascript",
                    "lines": lines,
                    "excluded": excluded,
                    "javascript": fragments,
                }
    return {"roots": {name: str(root) for name, root in roots.items()}, "files": files, "skipped": skipped}


def report(directory, passing_only=False, contexts=None):
    """
    Merge saved attempts and regenerate combined and runtime reports without executing tests
    """
    conversion = subprocess.run(
        ["node", str(Path(__file__).with_name("convert.cjs")), str(directory)],
        capture_output=True,
        text=True,
    )
    if conversion.returncode:
        (directory / "conversion.log").write_text(conversion.stdout + conversion.stderr)
        error = f"Frontend coverage conversion failed, see {directory / 'conversion.log'}"
        (directory / "collection.json").write_text(json.dumps({"errors": [error], "excluded": []}))
        raise RuntimeError(error)
    converted = json.loads((directory / "frontend.json").read_text())
    files = converted["inventory"]["files"]
    for filename, snapshot in files.items():
        if (
            Path(filename).exists()
            and hashlib.sha256(Path(filename).read_text().encode()).hexdigest() != snapshot["sha256"]
        ):
            raise ValueError(f"Source changed since coverage collection, {filename}")
    (directory / "report-inventory.json").write_text(json.dumps(converted["inventory"]))
    coverage = Coverage(data_file=str(directory / ".coverage"), config_file=False)
    coverage.set_option("run:plugins", ["pret.testing.coverage"])
    coverage.set_option("pret.testing.coverage:inventory", str(directory / "report-inventory.json"))
    coverage.erase()
    data = coverage.get_data()
    data.add_lines({filename: [] for filename in files})
    errors = converted["errors"]

    def add(runtime, test, status, hits):
        # All sources share the inventory while each context retains its actual execution runtime
        if passing_only and status != "passed":
            return
        data.set_context(f"{runtime}|{test}|status={status}")
        data.add_lines(
            {
                filename: set(lines) & set(files[filename]["lines"])
                for filename, lines in hits.items()
                if filename in files
            }
        )

    for attempt_dir in sorted((directory / "attempts").iterdir()):
        metadata = json.loads((attempt_dir / "attempt.json").read_text())
        if metadata["status"] == "incomplete":
            errors.append(f"Incomplete test attempt {metadata['test']}")
        error_file = attempt_dir / "errors.jsonl"
        if error_file.exists():
            errors.extend(json.loads(line)["error"] for line in error_file.read_text().splitlines())
        statuses = {}
        status_file = attempt_dir / "pytest-status.jsonl"
        if status_file.exists():
            for line in status_file.read_text().splitlines():
                result = json.loads(line)
                old = statuses.get(result["test"], "passed")
                outcomes = {old, result["status"]}
                statuses[result["test"]] = (
                    "failed" if "failed" in outcomes else "skipped" if "skipped" in outcomes else "passed"
                )
        if metadata["suite"] in {"client", "jupyter"} and not list((attempt_dir / "raw").glob("*.json")):
            errors.append(f"No frontend coverage saved for {metadata['test']}")
        for started in (attempt_dir / "python").glob("*.started"):
            process = json.loads(started.read_text())
            if not process.get("saved", False) or not (started.parent / process["data"]).is_file():
                errors.append(f"Python process did not save coverage, {started.name}")
        for filename in (attempt_dir / "python").rglob(".coverage*"):
            if filename.name.endswith(("-journal", "-wal", "-shm")):
                continue
            original = CoverageData(basename=str(filename))
            original.read()
            for context in original.measured_contexts():
                original.set_query_context(context)
                test = context or metadata["test"]
                status = metadata["status"]
                if metadata["suite"] == "pytest" and "|" in context:
                    nodeid = context.split("|", 1)[1].rsplit("|", 1)[0]
                    status = statuses.get(nodeid, status)
                add("python", test, status, {name: original.lines(name) or [] for name in original.measured_files()})
    for result in converted["results"]:
        add(result["runtime"], result["test"], result["status"], result["files"])
    data.add_file_tracers({filename: "pret.testing.coverage.SourcePlugin" for filename in files})
    coverage.save()
    for runtime in ["combined", "python", "browser", "node"]:
        pattern = "" if runtime == "combined" else rf"^{runtime}\|"
        if contexts:
            pattern = f"(?=.*(?:{contexts})){pattern}"
        coverage.html_report(directory=str(directory / runtime), show_contexts=True, contexts=[pattern])
    coverage.json_report(outfile=str(directory / "coverage.json"), show_contexts=True, contexts=[contexts or ""])
    (directory / "collection.json").write_text(
        json.dumps({"errors": errors, "excluded": converted["excluded"]}, indent=2)
    )
    print(f"Coverage report, {directory / 'combined' / 'index.html'}", flush=True)
    if errors:
        raise RuntimeError("Incomplete coverage collection\n" + "\n".join(errors))
