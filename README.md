![Tests](https://img.shields.io/github/actions/workflow/status/percevalw/pret/tests.yml?branch=main&label=tests&style=flat-square)
[![Documentation](https://img.shields.io/github/actions/workflow/status/percevalw/pret/docs.yml?branch=main&label=docs&style=flat-square)](https://percevalw.github.io/pret/latest/)
[![PyPI](https://img.shields.io/pypi/v/pret?color=blue&style=flat-square)](https://pypi.org/project/pret/)

# PRET

Pret is a library for building full-stack reactive user interfaces in Python, using React as a rendering engine.

## Installation

```bash
pip install pret pret-joy  --user
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
from pret.ui.joy import Checkbox, Input, Stack, Typography

store = create_store(
    {
        "faire à manger": True,
        "faire la vaisselle": False,
    },
    sync=True,
)


@component
def TodoApp():
    todos = use_store_snapshot(store)
    typed, set_typed = use_state("")
    num_remaining = sum(not ok for ok in todos.values())
    plural = "s" if num_remaining > 1 else ""

    def on_key_down(event):
        if event.key == "Enter":
            state[typed] = False
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
