# Widgets

Self-contained, interactive and controllable components.

## What’s a widget?

In Pret, a *widget* is a component that manages its own UI state, exposes a small imperative API (a “handle”) for commands like *scroll to X* or *focus*, and emits events (callbacks) when the user interacts with it. A widget runs on its own, without needing to be controlled by a parent component.

In dashboards and multi-pane tools, panes often look at the same data from different angles but don’t need to mirror each other’s UI details (scroll positions, hovers, transient filters, typing...): this is where widgets shine, as they encapsulate their own UI state and logic.

By contrast, a classic (controlled / presentational) component, such as [AnnotatedText][metanno.ui.AnnotatedText], keeps minimal or no state and expects its parent to hold all the UI logic and state and to pass it down as params.

## If it looks like a widget and quacks like a widget...

Widgets are not a formal class or type in Pret. They are just components that can run without being controlled by a parent. Just like any other component in Pret, widgets can be declared from inside a `@component` function (i.e., controlled by another component) or directly from your notebook/server. To interact with other parts of the UI, widgets expose two kinds of interfaces:

#### 1) Events (callbacks)

- Notify when an event occurs: `on_change(id)`, `on_hover(id, ...)`, ...
- These are callbacks: they don’t change other widgets directly.

#### 2) Imperative handle (commands)

- Let other parts of the UI command this widget: `handle.current.scroll_to_id(id)`, `handle.current.set_active(id)`, `handle.current.set_filter(key, value)`, ...
- These are UI actions and read-only getters like `handle.current.get_selection()`.

!!! tip "Actions object"

    In Pret, a common pattern is to pass a mutable `handle` Ref defined with [use_ref][pret.hooks.use_ref] that the widget fills: it writes callable entries under a `current` attribute, which the parent can then call imperatively.

## Do's and don'ts

- :no_entry_sign: Don’t wrap them in a stateful parent that is going to re-render often.
- :no_entry_sign: Don't make them expect props that change with the app state (e.g., `value`).
- :white_check_mark: You can compose them in a layout/panel/div for presentational purposes.
- :white_check_mark: You can control them from the notebook or from other widgets through their `handle`.

## Example

Our objective is to define a counter and a log that stand alone, and from the notebook decide if and how they talk to each other, without introducing a new "controller" (or "App") component. Controlled components (ie, non widgets components) push you to create a shared parent to pass props around. Widgets keep their own state, expose a small actions handle, and emit events so you can wire them together (or not) from the notebook.

Let's create a `CounterWidget` that exposes `reset` / `set` commands and an `on_change` event, and a `LogWidget` that exposes an `add` command.

```python
from pret import component
from pret.hooks import use_event_callback, use_ref, use_state, use_imperative_handle
from pret_joy import Button, Stack, Typography


@component
def CounterWidget(handle=None, on_change=None):
    count, set_count = use_state(0)

    use_imperative_handle(
        handle,
        lambda: {
            "reset": lambda: set_count(0),
            "set": set_count,
        },
        [],
    )

    @use_event_callback
    def increment():
        def update(prev):
            new_value = prev + 1
            if on_change is not None:
                on_change(new_value)
            return new_value

        set_count(update)

    return Button(
        f"Count: {count}",
        on_click=increment,
        spacing=1,
        sx={"minWidth": 220, "m": 1},
    )


@component
def LogWidget(handle=None):
    messages, set_messages = use_state([])

    use_imperative_handle(
        handle,
        lambda: {
            "add": lambda text: set_messages(lambda prev: [*prev, text]),
            "clear": lambda: set_messages([]),
        },
        [],
    )

    return Stack(
        [
            Typography("Logs:", level="body-md"),
            *[Typography(f"- {msg}", level="body-sm") for msg in messages],
        ],
        spacing=1,
        sx={"minWidth": 220, "m": 1},
    )
```

Let's render the counter widget in a cell:

```python { .render-with-pret }
# remote refs to hold the counter actions
counter_handle = use_ref()
log_handle = use_ref()

CounterWidget(
    handle=counter_handle,
    on_change=lambda v: log_handle.current.add(f"Counter is now {v}"),
)
```

Then the log widget in another cell:

```python { .render-with-pret }
LogWidget(handle=log_handle)
```

Finally the reset button in another cell:

```python { .render-with-pret }
Button(
    "Reset counter",
    on_click=lambda: counter_handle.current.reset(),
    color="neutral", variant="outlined",
    sx={"minWidth": 220, "m": 1},
)
```

We could also have everything in the same cell using a `Stack` or `Grid` layout.

You can observe that no UI state is shared between the two widgets: they talk and synchronize through events and commands.

## Remote refs

In the example above, we created two `use_ref()` references in the notebook to hold the handles of the two widgets. When these refs are created *in the notebook*, they are called *remote refs* because they are created on the server side and allow to control widgets running on the client side. `use_ref` is the **only** hook that can be used outside of a `@component` function, i.e., directly in the notebook.

One advantage of this is that, should you refactor your app and move the widget creation from the notebook to a component, and the *remote* ref to a standard *local* ref, you just have to move everything in a `@component` function, and it should work like a charm.

However, this comes with a few limitations :

- you can only interact with fields on the handle that are functions (e.g., :white_check_mark: `handle.current.focus()`), not properties (e.g., :no_entry_sign: `handle.current.value`).
- calling functions on remote refs is asynchronous, and the result is a future/promise. You cannot expect to get a return value immediately.

## Widget factories

As explained above, widgets are just components that follow some conventions. They should therefore not require access to the server/kernel state to be rendered. You may still need to configure a widget with some data from the server. This is where *widget factories* come in handy: a widget factory is a function that takes runs on the server and returns a Renderable widget that can be embedded in a Pret app.

For instance, imagine a `DataFrame` widget that displays the content of a pandas DataFrame. DataFrame are not serializable, so the widget cannot directly use a dataframe during its rendering. Instead, we can create a widget factory that takes a DataFrame, prepares the data (e.g., serializes it to JSON) and returns a [Table][metanno.ui.Table] configured with this data.

Observe how the factory function `DataFrameComponentFactory` is not decorated with `@component`: it is instead meant to run on the server and return a widget, which in turn can be rendered on the client.

Here is a *component factory* that takes a pandas DataFrame and returns a Table Renderable element:

```python
import pandas as pd
from pret.render import Renderable
from metanno import Table


def DataFrameStaticViewFactory(df: pd.DataFrame, editable_columns=[]) -> Renderable:
    # Prepare the data (e.g., serialize to JSON)
    data = df.to_dict(orient="records")
    columns = [
        {
            "name": col,
            "key": col,
            "filterable": True,
            "kind": "text"
            if df.dtypes[col].kind in "iufc"
            else "boolean"
            if df.dtypes[col].kind == "b"
            else "text",
            "editable": col in editable_columns,
        }
        for col in df.columns
    ]

    # Return a Table component configured with the data
    return Table(rows=data, columns=columns)
```

Note that since [Table][metanno.ui.Table] expects the data to be prepared and updated for it by its caller (i.e., it is a controlled component), we have no way make this component dynamic.

Now, here is a *widget factory* that takes a DataFrame and returns a configurable metanno [Table][metanno.ui.Table] widget. It will expose an imperative API to set/get filters and scroll to a given row and an event callback when a cell is changed:

```python
import pandas as pd
from pret.render import Renderable, component
from pret.hooks import use_event_callback, use_imperative_handle, use_ref, use_state
from pret import server_only
from metanno import Table


def DataFrameWidgetFactory(
    df: pd.DataFrame, handle=None, editable_columns=[]
) -> Renderable:
    # Prepare the data (e.g., serialize to JSON)
    data = df.to_dict(orient="records")
    columns = [
        {
            "name": col,
            "key": col,
            "filterable": True,
            "kind": "text"
            if df.dtypes[col].kind in "iufc"
            else "boolean"
            if df.dtypes[col].kind == "b"
            else "text",
            "editable": col in editable_columns,
        }
        for col in df.columns
    ]

    @server_only
    def handle_cell_change_server(row_id, row_idx, col_key, new_value):
        df.at[row_idx, col_key] = new_value

    @component
    def Widget(handle=None, on_cell_change=None) -> Renderable:
        # Internal state
        filters, set_filters = use_state({})
        table_handle = use_ref()
        state_data, set_state_data = use_state(data)

        use_imperative_handle(
            handle,
            lambda: {
                "set_filters": set_filters,
                "get_filters": lambda: filters,
                "scroll_to_row_idx": lambda idx, behavior=None: table_handle.current.scroll_to_row_idx(
                    idx, behavior
                ),
            },
            [],
        )

        @use_event_callback
        def handle_filters_change(filters, col):
            set_filters(filters)

        @use_event_callback
        def handle_cell_change(row_id, row_idx, col_key, new_value):
            # Update local state to reflect the change
            updated_data = list(state_data)
            updated_data[row_idx] = {**updated_data[row_idx], col_key: new_value}
            set_state_data(updated_data)
            if on_cell_change is not None:
                on_cell_change(row_id, row_idx, col_key, new_value)

        # Return a Table component configured with the data and event handlers
        return Table(
            rows=state_data,
            columns=columns,
            filters=filters,
            auto_filter=True,
            on_filters_change=set_filters,
            on_cell_change=handle_cell_change,
            handle=table_handle,
            style={"height": "200px"},
        )

    return Widget(handle=handle, on_cell_change=handle_cell_change_server)
```

You can now use it by first creating a reference to hold the widget handle, then creating the widget using the factory, and finally rendering it:

```python { .render-with-pret }
df = pd.DataFrame([{"a": i, "check": False} for i in range(100)])
handle = use_ref()
DataFrameWidgetFactory(df, handle=handle)
```

Again : like widgets, a widget factory merely a code pattern: it is a function that runs on the server and returns a Renderable widget that can be rendered and controlled from the outside.
Note how changing a cell in the table updates the underlying DataFrame on the server side.
You can also control it imperatively by running the following code in another cell:

```python { .no-exec }
# Scroll to row 50
handle.current.scroll_to_row_idx(50)
```

or from a button:

```python { .render-with-pret }
from pret_joy import Button

Button(
    "Go to row 50",
    on_click=lambda: handle.current.scroll_to_row_idx(50),
    sx={"m": 1},
)
```
