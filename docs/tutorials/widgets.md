# Widgets

Small, self-contained, interactive components.

## What’s a widget?

In Pret, a *widget* is a component that manages its own UI state, exposes a small imperative API (a “handle”) for commands like *scroll to X* or *focus*, and emits events (callbacks) when the user interacts with it. Think: data grid, code editor, map, media player, annotated text viewer, etc.

In dashboards and multi-pane tools, panes often look at the same data from different angles but don’t need to mirror each other’s UI details (scroll positions, hovers, transient filters, typing...): this is where widgets shine, as they encapsulate their own UI state and logic.

By contrast, a classic (controlled / presentational) component, such as [AnnotatedText][metanno.ui.AnnotatedText], keeps minimal or no state and expects its parent to hold all the UI logic and state and to pass it down as params.

## If it looks like a widget and quacks like a widget...

Widgets are not a formal class or type in Pret. They are just components that can run without being controlled by a parent. To interact with other parts of the UI, widgets expose two kinds of interfaces:

#### 1) Events (callbacks)

- Notify the host: `onActiveChange(id)`, `onAdd(item)`, `onDelete(id)`, ...
- These are callbacks: they don’t change other widgets directly.

#### 2) Imperative handle (commands)

- Let other parts of the UI command this widget: `scrollToId(id)`, `setActive(id)`, `setFilter(key, value)`, ...
- These are UI actions and read-only getters like `getSelection()`.

!!! tip "Actions object"

    In Pret, a common pattern is to pass a mutable `actions` object that the widget fills:
    it writes callable entries into it (your “handle”).

## Do's and don'ts

- :white_check_mark: Compose them in a layout/panel/div without putting extra state in the container.
- :white_check_mark: Wire events to commands (as above). That’s the only coupling you need.
- :no_entry_sign: Don’t wrap them in a stateful parent that is going to re-render often.

## Example

Let's create two widgets, TBC

```python
```

You can observe that no UI state is shared between the two widgets: they talk and synchronize through events and commands.

## Widget factories

As explained above, widgets are just components that follow some conventions. They should therefore not require access to the server/kernel state to be rendered. You may still need to configure a widget with some data from the server. This is where *widget factories* come in handy: a widget factory is a function that takes runs on the server and returns a Renderable widget that can be embedded in a Pret app.

For instance, imagine a `DataGrid` widget that displays the content of a pandas DataFrame. DataFrame are not serializable, so the widget cannot directly use a dataframe during its rendering. Instead, we can create a widget factory that takes a DataFrame, prepares the data (e.g. serializes it to JSON) and returns a [Table][metanno.ui.Table] configured with this data.

Observe how the factory function `DataFrameComponentFactory` is not decorated with `@component`: it meant to run on the server and return a widget that can be rendered on the client.

Here is a *component factory* that takes a pandas DataFrame and returns a Table Renderable element:

```python
import pandas as pd
from pret.render import Renderable
from metanno import Table


def DataFrameComponentFactory(df: pd.DataFrame) -> Renderable:
    # Prepare the data (e.g. serialize to JSON)
    data = df.astype(str).to_dict(orient="records")
    columns = [{"name": col} for col in df.columns]

    # Return a Table component configured with the data
    return Table(data=data, columns=columns)
```

Now, here is a *widget factory* that takes a DataFrame and returns a configurable Table widget:

```python
import pandas as pd
from pret.render import Renderable, component
from pret.hooks import use_state, use_event_callback
from metanno import Table


def DataFrameWidgetFactory(df: pd.DataFrame) -> Renderable:
    # Prepare the data (e.g. serialize to JSON)
    data = df.astype(str).to_dict(orient="records")
    columns = [{"name": col} for col in df.columns]

    @component
    def Widget(on_filter_change=None, ref=None) -> Renderable:
        # Internal state
        filters, set_filters = use_state({})

        if ref is not None:
            ref.current = {
                "set_filters": set_filters,
                "get_filters": lambda: filters,
            }

        @use_event_callback
        def handle_filters_change(filters, col):
            set_filters(filters)
            if on_filter_change:
                on_filter_change(filters, col)

        # Return a Table component configured with the data and event handlers
        return Table(
            data=data,
            columns=columns,
            filters=filters,
            on_filter_change=set_filters,
        )
```
