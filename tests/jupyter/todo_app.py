"""
Render small PRET components used by notebook rendering and recovery tests
"""

from pret import component, use_connection_status, use_store_snapshot
from pret.react import Fragment, div, input, label, p, span


@component
def TodoApp(state):
    """
    Display shared checkbox state and the live kernel connection status
    """
    connection = use_connection_status()
    todos = use_store_snapshot(state)
    remaining = sum(not ok for ok in todos.values())
    plural = "s" if remaining > 1 else ""

    return div(
        span("online" if connection["connected"] else "offline", id="connection-status"),
        *(
            div(
                input(
                    checked=ok,
                    type="checkbox",
                    id=todo.replace(" ", "-"),
                    on_change=lambda event, text=todo: state.update({text: event.target.checked}),
                ),
                label(todo, **{"for": todo.replace(" ", "-")}),
            )
            for todo, ok in todos.items()
        ),
        p(f"Number of unfinished todo{plural}: {remaining}"),
        id="todoapp",
    )


@component
def TodoList(state):
    """
    Render a registered React component whose Store edits also update RemainingTodoCounter
    """
    todos = use_store_snapshot(state["todos"])
    return Fragment(
        *(
            label(
                input(
                    type="checkbox",
                    checked=todo["done"],
                    on_change=lambda event, idx=idx: state["todos"][idx].update({"done": event.target.checked}),
                ),
                todo["text"],
            )
            for idx, todo in enumerate(todos)
        )
    )
