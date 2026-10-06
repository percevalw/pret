"""
Run Python and Playwright tests or regenerate saved coverage reports
"""

import argparse
import signal
import sys
from pathlib import Path


def main():
    """
    Select component tests from the current directory for the shared runner
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "report"])
    parser.add_argument("--coverage", action="store_true")
    parser.add_argument("--suite", choices=["all", "python", "client", "jupyter"], default="all")
    parser.add_argument("--match", help="pytest expression or Playwright title regular expression")
    parser.add_argument("--browser", action="append", choices=["chromium", "firefox", "webkit"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--passing-only", action="store_true")
    parser.add_argument("--contexts")
    args = parser.parse_args()
    directory = (
        args.output or Path("coverage" if args.coverage or args.command == "report" else "test-results")
    ).resolve()
    if args.command == "report":
        from .coverage import report

        report(directory, args.passing_only, args.contexts)
        return 0
    if args.coverage and args.browser and args.browser != ["chromium"]:
        parser.error("V8 coverage requires --browser chromium")
    from .runner import run

    # Unwind server and child process cleanup when a job timeout stops the runner
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
    return run(Path.cwd(), directory, args.suite, args.match, args.browser or ["chromium"], args.coverage)


raise SystemExit(main())
