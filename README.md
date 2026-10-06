![Tests](https://img.shields.io/github/actions/workflow/status/percevalw/pret/tests.yml?branch=main&label=tests&style=flat-square)
[![Documentation](https://img.shields.io/github/actions/workflow/status/percevalw/pret/docs.yml?branch=main&label=docs&style=flat-square)](https://percevalw.github.io/pret/latest/)
[![PyPI](https://img.shields.io/pypi/v/pret?color=blue&style=flat-square)](https://pypi.org/project/pret/)
[![pret](https://labextensions.dev/api/badge/pret?metric=downloads&leftColor=%23555&rightColor=%23F37620&style=flat)](https://labextensions.dev/extensions/pret)
[![Binder](https://mybinder.org/badge_logo.svg)](https://mybinder.org/v2/gh/percevalw/pret/main?urlpath=%2Fdoc%2Ftree%2Fexamples%2FTodos.ipynb)

# PRET

Pret lets you build interactive interfaces in Python, inside notebooks or as standalone web applications.
Use a Store to share data between components, connect edits to Python and save your work.
The interface runs in the browser using React.

## Installation

```bash
pip install pret pret-joy
```

To use it with Jupyter, if you install the library in a custom environment (conda, venv, or other),
you will likely need to tell Jupyter where to find the front-end files.
You can do this by running the following command (only once):

```bash
pret update-jupyter-config --apply
```

## Features

- **Python, only Python**: pret is written in Python: you can write your both your UI and server actions Python. No need to learn a new language.
- **Client-side rendering**: unlike other Python UI frameworks, pret runs primarily in the browser. This enables a fast response time to user actions (like hover events), and a better user experience under degraded network conditions.
- **Built on React**: pret uses React as a rendering engine, and benefits from its ecosystem.
- **Reactive**: unlike other solutions like ipywidgets, pret is reactive. Only the parts of the UI that need to be updated are re-rendered.
- **State management**: in addition to React's local state management (i.e. `use_state`), pret provides a global and modular state management solution that is synchronized between components, between the browser and the server, and can even be persisted to a file for collaborative editing.
- **Modular**: pret is designed to be modular. You can easily create your own components, and reuse them in other pret-based projects.
- **Integrated with Jupyter**: pret components can be used in Jupyter notebooks, as well as in standalone web applications.
- **Remote execution**: pret can call and use the result of Python functions on the server from the browser

## Use it in a notebook

Let's write a simple todo app that should:

- display a list of todos, that can be checked as done
- display the number remaining todos
- change the font to bold as a todo is hovered
- allow editing the todo list directly in Python

Copy and paste the following code in a notebook:

```python { .render-with-pret }
from pret import component, create_store, run, use_state, use_store_snapshot
from pret_joy import Checkbox, Input, Stack, Typography

store = create_store(
    {
        "faire à manger": True,
        "faire la vaisselle": False,
    },
)


@component
def TodoApp():
    todos = use_store_snapshot(store)
    typed, set_typed = use_state("")
    num_remaining = sum(not ok for ok in todos.values())
    plural = "s" if num_remaining > 1 else ""

    def on_key_down(event):
        if event.key == "Enter":
            store[typed] = False
            set_typed("")

    return Stack(
        *(
            Checkbox(
                label=todo,
                checked=ok,
                on_change=lambda e, t=todo: store.update({t: e.target.checked}),
            )
            for todo, ok in todos.items()
        ),
        Input(
            value=typed,
            on_change=lambda event: set_typed(event.target.value),
            on_key_down=on_key_down,
            placeholder="Add a todo",
        ),
        Typography(
            f"Number of unfinished todo{plural}: {num_remaining}",
            sx={"minWidth": "230px"},  # just to avoid jittering when it's centered
        ),
        spacing=2,
        sx={"m": 1},
    )


TodoApp()
```

In comparison, the closest alternative using ipywidgets looks like the following snippet:

<details>
<summary>IPyWidget's implementation</summary>

```python { .no-exec }
import ipywidgets as widgets

state = {
    "faire à manger": True,
    "faire la vaisselle": False,
}


class IPWTodoApp:
    def __init__(self):
        self.box = widgets.VBox()
        self.render()

    def _repr_mimebundle_(self, *args, **kwargs):
        return self.box._repr_mimebundle_(*args, **kwargs)

    def render(self, *args, **kwargs):
        num_remaining = sum([not checked for _, checked in state.items()])
        plural = "s" if num_remaining > 1 else ""

        def on_input_submit(sender):
            state[input_widget.value] = False
            self.render()

        def create_todo_item(todo, checked):
            def update_todo_status(*args, **kwargs):
                state[todo] = checkbox.value
                self.render()

            checkbox = widgets.Checkbox(
                value=checked,
                description=todo,
                disabled=False,
                indent=False,
            )
            checkbox.observe(update_todo_status, names="value")
            return checkbox

        input_widget = widgets.Text(
            placeholder="Add a todo",
            description="",
            disabled=False,
        )
        input_widget.on_submit(on_input_submit)

        self.box.children = [
            *(create_todo_item(todo, checked) for todo, checked in state.items()),
            input_widget,
            widgets.Label(value=f"Number of unfinished todo{plural}: {num_remaining}"),
        ]


IPWTodoApp()
```
</details>

You also lose some features:

- the app stops working if the server shuts down
- hover events cannot be listened to
- no React dom diffing: the app must either be re-rendered entirely (as in the example),
  or you must determine specifically which field of which widget to update

## Use it in a standalone app

You can also use pret to build standalone web applications. Copy the above code in a file
named `app.py`, and change the last line to

```python
if __name__ == "__main__":
    run(TodoApp)
```

Then, run the following command, and voilà !

```bash
python app.py
```


## Tables and saved work

A Store can contain ordinary Python values and DataFrames. Add a pandas table to work on an editable
copy, or a Spark or Ibis query to browse its source. Connected widgets request rows as needed.

`save_store(store, "review.pret")` creates a SQLite file and returns a Store whose subsequent edits
are saved automatically. `load_store("review.pret")` reopens it, including the saved table rows.

- [Work with DataFrames](https://percevalw.github.io/pret/latest/tutorials/dataframes/), from pandas to external queries
- [Save and reopen your work](https://percevalw.github.io/pret/latest/tutorials/saving-stores/), including shared files
- [Inside a Store file](https://percevalw.github.io/pret/latest/under-the-hood/store-files/), what a `.pret` file contains and why it is SQLite
- [How widgets query DataFrames](https://percevalw.github.io/pret/latest/under-the-hood/dataframe-queries/), paging, previews and custom sources

## Tests

Run from the Pret checkout with Python 3.10 or 3.11, Node 22, `.[testing]` installed and built Pret assets

```sh
python -m pret.testing run
python -m pret.testing run --suite python --match persistence
python -m pret.testing run --suite client
python -m pret.testing run --suite jupyter --browser firefox
```

| Group | Location | Checks |
| --- | --- | --- |
| Python | `tests/python/test_*.py` | Persistence, DataFrame queries and exact types, database history, serialization |
| Client | `tests/client/*.spec.ts` | Cache and failure handling, browser Arrow and SQLite conversions, standalone editing |
| JupyterLab | `tests/jupyter/*.spec.ts` | Rendering, Store edits and undo, saved data between views, rejected edits, full page display |

The client and JupyterLab groups use Playwright. Each JupyterLab case starts a fresh server, kernels and workspace,
using small Pret application fixtures. Successful workspaces are removed, failed cases keep logs, screenshots and traces
in `test-results/attempts`. `--match` accepts a pytest expression or a Playwright title regular expression.
Repeat `--browser` to select multiple browsers, Chromium is the default and WebKit is optional.
CI runs Python 3.10 and 3.11 with JupyterLab 3 and 4 on Chromium and Firefox, including SQLite and Spark checks.

## Test coverage

Coverage uses the same cases and assertions. Build source maps first, then run with Python 3.11, JupyterLab 4 and Chromium

```sh
npm install
uv pip install 'jupyterlab>=3.6,<4' hatchling hatch-jupyter-builder 'setuptools<82'
PRET_COVERAGE=1 uv pip install --no-build-isolation -e '.[coverage]'
uv pip install 'jupyterlab>=4.4,<4.5' 'ibis-framework[sqlite,pyspark]>=12,<13' 'pyspark>=3.5,<4'
npx playwright install chromium
python -m pret.testing run --coverage
```

Spark checks need Java 17. The extension build uses JupyterLab 3 to match its builder dependencies.
Open `coverage/combined/index.html`. The Python, browser and Node reports use the same source inventory,
including untouched files. Covered lines show their test, runtime, attempt and result.

```sh
python -m pret.testing run --coverage --suite jupyter --match rendering --output coverage/rendering
python -m pret.testing report --output coverage
python -m pret.testing report --output coverage --passing-only --contexts '^browser\|'
```

Reports include failed tests by default. Regeneration uses saved sources and raw ranges without rerunning tests,
and rejects changed source content. `coverage.json` contains line contexts, `collection.json` lists exclusions and
collection errors. Test failures and incomplete collection fail the command, percentages are informational.

Coverage measures original Python, JavaScript, TypeScript and literal JavaScript overrides inside Python files.
Dynamic JavaScript has generated source entries with its originating call site in `report-inventory.json`.
Dependencies, compiler helpers and bytecode reconstructed functions are excluded. Notebook functions without source
files execute normally but are excluded from file coverage. Use a normal package build to restore assets without maps.
