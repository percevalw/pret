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
