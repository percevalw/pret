# Contributing

## Installation

Pret needs Node.js 22 and Python 3.10 or later. Its browser assets are built with the JupyterLab 3 builder.

```bash
git clone --recursive https://github.com/percevalw/pret.git
cd pret
npm install
pip install 'jupyterlab>=3.6,<4' hatchling editables hatch-jupyter-builder
pip install --no-build-isolation -e '.[testing]' --group dev
```

## Tests

```bash
python -m pret.testing run
python -m pret.testing run --suite python --match persistence
python -m pret.testing run --suite jupyter --browser firefox
```

The `python` suite runs `tests/python` with pytest, `client` runs `tests/client` in a browser with Playwright, and
`jupyter` runs `tests/jupyter` with a fresh JupyterLab server for each test. Failed tests keep their logs, screenshots
and traces in `test-results/attempts`.

## Test coverage

```bash
PRET_COVERAGE=1 pip install --no-build-isolation -e '.[coverage]'
python -m pret.testing run --coverage
```

Open `coverage/combined/index.html` to see the Python, JavaScript and TypeScript lines run by each test.

## Releasing

Changes are listed under `## Unreleased` in `changelog.md`. To release them, run the **Prepare Release PR** workflow from
the Actions tab with a `version`, eg `0.7.0` or `0.7.0-beta.2`, or a `bump` (`patch`, `minor` or `major`). It opens a
`release/v<version>` PR that bumps the version and dates the changelog section, the same as running locally:

```bash
python scripts/prepare_release.py 0.7.0 --commit --push --pr
```

The PR description holds the release message between `release-message` markers and can be edited before merging.
Merging the PR creates the `v<version>` GitHub release with this message, a pre-release for versions like
`0.7.0-beta.2`, which then publishes the package to PyPI. Both workflows need a `RELEASE_PAT` repository secret, a
token allowed to push branches, open PRs and create releases: a release created with the default workflow
token would not start the PyPI workflow.
