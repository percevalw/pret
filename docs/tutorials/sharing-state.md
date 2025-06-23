# Sharing state

In the previous tutorial, we have seen how to compose a simple component from other components, how to render it, and detect user events. In this tutorial, we will see how to share state between components.

## Stores

Why is state management hard in web development? The dynamic nature of user interfaces means multiple components must reflect and react to shared and changing data without re-rendering everything anytime something changes (e.g, you recompute the whole app UI whenever a single state variable changes), or convoluted data flows (e.g., state being passed through many layers of components that don’t even use it). Traditional approaches, like Redux, often introduce layers of boilerplate and require careful architecture when planning mutations on the app data.

There is another issue of immutability: we cannot mutate the state directly (e.g., `state.todos[0]["done"] = True`), since React, and thus Pret, relies on shallow comparison to detect changes in the state. For instance, if `todos` is the same object, even though its content has changed, React will consider that the state has not changed and will not trigger a re-render.

And if we take care of preventing direct mutations, changing the state can be cumbersome. For instance, if we want to change the `done` field of the first todo, we would have to do something like this:

```{ .python .no-exec }
new_todos = list(todos)
new_todos[0] = {**todos[0], "done": True}

# We now have todos != new_todos and
# todos[i] == new_todos[i] for all i except 0
```

Pret takes inspiration from [valtio](https://github.com/pmndrs/valtio/) and provides a simple way to manage state in your components. A store can be created and shared between components. Access to the store data and mutations are recorded, such that the app knows which component should be re-rendered when a given part of the state changes.

To create a store, we use the `create_store` wrapper:

```python
from pret import create_store
from pret.store import subscribe

store = create_store(
    {
        "todos": [
            {"text": "My first todo", "done": True},
            {"text": "My second todo", "done": False},
        ],
        "letters": ["a", "b"],
    }
)

subscribe(store, callback=lambda ops: print(ops))
store["todos"][1]["done"] = True
# Out: [('set', ['todos', 1, 'done'], True, False)]

del store["todos"][1]["done"]
# Out: [('delete', ['todos', 1, 'done'], None, True)]

store["todos"][1]["cool"] = True
# Out: [('set', ['todos', 1, 'cool'], True, None)]

store["letters"].append("c")
# Out: [('set', ['letters', 2], 'c', None)]

store["letters"][1] = "z"
# Out: [('set', ['letters', 1], 'z', None)]  # (1)!
```

1. In Python, we cannot access the replaced value (None below). However, it is accessible in transpiled Python code (ie when the method is called from a @component function)

!!! warning "Supported types"

    At the moment, not all types can be used in a Pret store. We focus on supporting the most common container types, such as lists and dictionaries, in addition to the basic types (int, float, str, bool, None).

## Using stores in components

Now that we have a store object, we can use it in our components. To let Pret know that a component should re-render when a part of the state changes, we use the `use_store_snapshot` hook, which returns a snapshot of the state. This hook tracks access made on the state, and if a mutation on a part of the state that was accessed is detected :

- the component will re-render
- the snapshot will be different from the previous one (meaning, we don't have the `new_todos is todos` issue mentioned earlier)

```python { .render-with-pret }
from pret import component, use_store_snapshot, create_store
from pret.ui.joy import Checkbox, Stack

store = create_store({
    "todos": [
        {"text": "My first todo", "done": True},
        {"text": "My second todo", "done": False},
    ],
})


@component
def TodoList():  # (1)!
    todos = use_store_snapshot(store["todos"])

    def on_change(event, i):
        store["todos"][i]["done"] = event.target.checked

    return Stack(
        [
            Checkbox(
                label=todo["text"],
                checked=todo["done"],
                on_change=lambda event, i=i: on_change(event, i),
            )
            for i, todo in enumerate(todos)
        ],
        spacing=2,
    )


TodoList()
```

1. Note that we don't pass the `todos` as an argument to the `TodoList` component anymore. Instead, we use the `use_store_snapshot` hook to directly subscribe to the global `store` object.


## Sharing state between components

Sharing state between components is now straightforward.
Let's display the number of remaining todos in the list. We will
use the same `store` object as the component above.

```python { .render-with-pret }
from pret.ui.joy import Typography
from pret.ui.react import br

@component
def RemainingTodoCounter():
    todos = use_store_snapshot(store["todos"])
    num_remaining = sum(not todo["done"] for todo in todos)

    return Typography(
        f"Number of unfinished todos: {num_remaining}.",
        br(),
        "Click todos in the previous component to change the count.",
    )

RemainingTodoCounter()
```
