"""
Start process coverage before application imports and preserve pytest child data for offline reports
"""

import json
import os
import sys
from pathlib import Path

# The multiprocessing resource tracker runs only standard library housekeeping
if (os.environ.get("COVERAGE_PROCESS_START") or os.environ.get("COVERAGE_PROCESS_CONFIG")) and not any(
    argument.startswith("from multiprocessing.resource_tracker import main;") for argument in sys.orig_argv
):
    import coverage

    collector = coverage.process_startup() or coverage.Coverage.current()
    if collector is not None:
        marker = Path(os.environ["PRET_COVERAGE_DIR"]) / "python" / f"{os.getpid()}.started"
        marker.write_text(json.dumps({"saved": False, "command": sys.orig_argv}))
        child_data = None
        if os.environ.get("PYTEST_CURRENT_TEST"):
            collector.switch_context(os.environ["PYTEST_CURRENT_TEST"].rsplit(" ", 1)[0] + "|run")
            child_data = marker.parent / str(os.getpid()) / ".coverage"
            child_data.parent.mkdir()
        save = collector.save

        def save_process():
            """
            Save a completion marker and keep child data before pytest removes merged parallel files
            """
            save()
            filename = collector.get_data().data_filename()
            if child_data is not None:
                data = coverage.CoverageData(basename=str(child_data))
                data.erase()
                data.loads(collector.get_data().dumps())
                data.write()
                filename = str(child_data)
            marker.write_text(
                json.dumps(
                    {
                        "data": str(Path(filename).relative_to(marker.parent)),
                        "saved": True,
                        "command": sys.orig_argv,
                    }
                )
            )

        collector.save = save_process


def pytest_runtest_logreport(report):
    """
    Record pytest case outcomes without importing application packages before pytest coverage starts
    """
    filename = Path(os.environ["PRET_COVERAGE_DIR"]) / "pytest-status.jsonl"
    with filename.open("a") as output:
        output.write(json.dumps({"test": report.nodeid, "phase": report.when, "status": report.outcome}) + "\n")
