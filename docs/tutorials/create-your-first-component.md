# Your first component

Let's create a first UI component with Pret. We will start simple, with the "Hello World" of UI components: a Todo list.

Pret is a declarative UI library, which means that you describe the UI you want, and Pret takes care of rendering it for you.
Under the hood, we use React and React libraries to render the UI.

## Composing components

Our app should be able to display a list of todos, where each todo is described by a text and a boolean indicating whether it is done or not.
Let's use Joy's Checkbox for this:

```{ .python .render-with-pret }
from pret.ui.joy import Checkbox

Checkbox(
    label="My first todo",
    checked=True,
)
```

Great ! We successfully declared and rendered our first component. Let's make it a list. We will use the Stack component to stack multiple components vertically. To compose components, we pass checkboxes as positional arguments (or a list) to the Stack component, and Pret will render them as children of the Stack component.

```{ .python .render-with-pret }
from pret.ui.joy import Checkbox, Stack

Stack(
    Checkbox(label="My first todo", checked=True),
    Checkbox(label="My second todo", checked=False),
)
```

Instead of hardcoding the todos, we can use a list of todos and a loop to render them:

```{ .python .render-with-pret }
from pret.ui.joy import Checkbox, Stack

todos = [
    {"text": "My first todo", "done": True},
    {"text": "My second todo", "done": False},
]

Stack(
    [
        Checkbox(label=todo["text"], checked=todo["done"])
        for todo in todos
    ],
    spacing=2,
)
```

We can turn this into a TodoList component, so that we can reuse it later:

```{ .python .render-with-pret }
from pret import component


@component
def TodoList(todos):
    return Stack(
        [
            Checkbox(label=todo["text"], checked=todo["done"])
            for todo in todos
        ],
        spacing=2,
    )


TodoList(todos=todos)  # (1)!
```

1. Here, `todos` are not children components but parameters of the `TodoList` component, also known as `props` in React, so we pass them as keyword arguments. In fact, passing them as positional arguments would raise an error.

## Reacting to events

Now that we have a list of todos, we want to be able to mark them as done or not. We can use the `on_change` event of the Checkbox component to react to changes. For now, let's just make a popup appear when a todo is checked or unchecked.

```{ .python .render-with-pret }
from pret.ui.joy import Checkbox, Stack


def on_change(event):
    checked = event.target.checked
    alert(f"Todo {'checked' if checked else 'unchecked'}")


Checkbox(
    label="My first todo",
    checked=True,
    on_change=on_change,
)
```

## Adding state

Our app is still a bit static : you may have noticed that you cannot change the value of the checboxes. We need to add state to our app to keep track of the todos' state. Let's start simple by making a Counter component that increments a counter each time a button is clicked. We can use the `use_state` hook, which allows us to create a state variable that will persist across renders (calls of our component) and trigger a re-render when its value changes.

```{ .python .render-with-pret }
from pret.ui.joy import Button, Typography, Stack
from pret import component, use_state


@component
def Counter():
    count, set_count = use_state(0)

    def increment(event):
        set_count(count + 1)

    return Stack(
        [
            Button("Increment", on_click=increment),
            Typography(f"Count: {count}"),
        ],
        spacing=2,
    )

Counter()
```

As you can see, every time you click the button, the state changes which triggers a re-render of the component. This is how we can make our TodoList component interactive. We will use the `use_state` hook to keep track of the todos' state.

```{ .python .render-with-pret }
from pret.ui.joy import Checkbox, Stack
from pret import use_state, component

todos = [
    {"text": "My first todo", "done": True},
    {"text": "My second todo", "done": False},
]

@component
def TodoList(todos):
    todos, set_todos = use_state(todos)

    def on_change(event, index):
        new_todos = list(todos)
        new_todos[index] = {**todos[index], "done": event.target.checked}
        set_todos(new_todos)

    return Stack(
        [
            Checkbox(
                label=todo["text"],
                checked=todo["done"],
                on_change=(lambda index: lambda event: on_change(event, index))(index),
            )
            for index, todo in enumerate(todos)
        ],
        spacing=2,
    )

TodoList(todos=todos)
```
