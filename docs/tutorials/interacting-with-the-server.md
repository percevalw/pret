# Interacting with the server

In this tutorial, we'll see the difference between server-side and client-side code in Pret, and how to interact with the server from your app.

## Client-side environment

When you build a Pret app, you are building a web application that will run in the browser. By default, in Pret, any function used in a rendered component will be sent to the browser and executed with Pyodide. While this is a powerful
feature, this means that this function doesn't have access to your server-side environment, such as your database, filesystem or computing resources.

For instance, let's take a look at the following component:

```{ .python .render-with-pret }
from pret import component
from pret.ui.react import br
import os

static_cwd = os.getcwd()

def dynamic_cwd():
    return os.getcwd()

@component
def ShowCurrentWorkingDirectory():
    return [
        f"Current working dir when this App was built: {static_cwd}",
        br(),
        f"Current working dir when this App is rendered: {dynamic_cwd()}",
    ]

ShowCurrentWorkingDirectory()
```

The "dynamic" displayed path will be the one from the browser's pyodide environment. In fact, these docs are hosted as a static website on GitHub Pages, so there is no server-side environment to access *during the rendering process*. The "static" path will be the one from the server-side environment, computed when the component is built and sent as a constant to the browser.

## Running server-side code

To tell Pret to run a function on the server-side, you can decorate a function with `@server_only`. In this case, any call to this function from a client-side function will actually trigger a call to the server, and the result will be sent back to the client asynchronously.

!!! note "Async functions"

    Any function decorated with `@server_only` becomes an async function from the client's perspective. This means that you must use `await` on the result of this function to get the actual return value.

At the moment, it is not possible to directly await the result of the server function in the rendering function. Therefore, in the example below, we combine `@server_only` with `use_effect` to update the displayed current server working directory once it has been fetched from the server.

```python { .no-exec }
from pret import server_only, use_effect, use_memo, use_state
from pret import component
from pret.ui.react import br
import os
from asyncio import sleep

@server_only
def dynamic_server_cwd():
    return os.getcwd()

def dynamic_client_cwd():
    return os.getcwd()

static_cwd = os.getcwd()

@component
def ShowCurrentWorkingDirectory():
    server_cwd, set_server_cwd = use_state("undefined")

    async def on_load():
        set_server_cwd(await dynamic_server_cwd())

    use_effect(on_load, [])

    return [
        f"Current working dir when this App was built: {static_cwd}",
        br(),
        f"Current CLIENT working dir when this App is rendered: {dynamic_client_cwd()}",
        br(),
        f"Current SERVER working dir when this App is rendered: {server_cwd}",
    ]

ShowCurrentWorkingDirectory()
```

Since this app is hosted on GitHub Pages, there is no server-side environment to access. However, you can run this code in a notebook to see the difference between the client and server working directories.


## Synchronizing client and server-side states

In the last [Sharing state]("./sharing-state.md") tutorial, we saw how to create a shared state between components with `state = proxy(...)`. This shared state is stored in the browser's memory, and is not accessible from the server. This means that once you have executed your app, the `state` variable in your notebook will not be updated when the state in the browser is updated.

Pret offers a simple way to synchronize the state between the client and the server, by using the `remote_sync` option in the `proxy` function. This option will keep both server and client states in sync whenever one of them is updated.

```python { .render-with-pret }
from pret.ui.joy import Button, Typography, Stack
from pret import component, proxy
from pret.hooks import use_tracked

state = proxy({
    "count": 0,
}, remote_sync=True)

@component
def Counter():
    tracked = use_tracked(state)

    def increment(event):
        state["count"] += 1

    return Button(f"Count: {tracked['count']}. Click to increment", on_click=increment)

Counter()
```

In your notebook, you can now change the `state["count"]` variable in another cell, and observe the change in the browser. Conversely, you can click the "Increment" button in the browser, and print the `state["count"]` variable in your notebook to see the change.

```python
# Show the current count, synchronized with the browser
print(state["count"])

# Change the count from the notebook
state["count"] = 42
```
