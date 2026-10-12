# How Python components run in the browser

When you display a Pret component in a notebook, the function you wrote does not run in the kernel but in the
browser, every time React renders it. Running it in Python would mean a round trip to the kernel for each click or
keystroke. This page explains how Pret makes this work: how the Python code becomes JavaScript, how the values it
uses travel with it, what a notebook output actually contains, and how the browser calls back into Python when a
function has to run there.

## Components are React function components

Pret follows the model of [React](https://react.dev/): a component is a function that returns elements, and React
calls it again whenever its state changes. Elements such as `div` and `button` from `pret.react`, and hooks such as
`use_state`, are Python stubs whose browser version calls React directly. For instance, `use_state` is React's own
`useState`. We will follow this component through the rest of this page:

```python
from pret import component, use_state
from pret.react import button, div

greeting = "Hello"


def label(count):
    return f"{greeting}, clicked {count} times"


@component
def Counter():
    count, set_count = use_state(0)
    return div(button(label(count), on_click=lambda event: set_count(count + 1)))


Counter()
```

When you click the button, the state changes and React calls `Counter` again in the browser, which calls `label`
again, without contacting the kernel.

## Python code is translated into JavaScript

[Pyodide](https://pyodide.org/) could run Python itself in the browser, but it has to download and start a full
Python runtime, and every call between React and Python code would go through WebAssembly. Pret instead translates
the functions to JavaScript with [Transcrypt](https://www.transcrypt.org/), so that they run as fast as ordinary
JavaScript and call React directly.

To do so, Pret needs the source of each function. It reads it with `inspect.getsource` when possible. Otherwise, for
instance for a lambda, whose source line also contains the code around it, or for a function created with `exec`,
it rebuilds the source from the bytecode with [pygetsource](https://github.com/percevalw/pygetsource). Transcrypt
then turns `label` into the following code, keyword argument handling aside:

```javascript
var label = function (count) {
    return '{}, clicked {} times'.format(greeting, count);
};
```

A few standard modules come translated in advance, such as `math`, `re`, `datetime`, `json`, `itertools` and
`functools`. Functions from other modules cannot run in the browser: see
[Interacting with the server](../tutorials/interacting-with-the-server.md) for these.

!!! note "What differs from Python"

    The translated code keeps JavaScript semantics in a few places. An empty list or dict is true in an `if`, since
    Transcrypt turns `if items:` into `if (items)`: test `len(items)` instead. `value is None` becomes
    `value === null`, which does not match the `undefined` that JavaScript returns for a missing prop or key.

## Captured values travel with the function

A function rarely stands alone: `label` reads the global `greeting`, and `Counter` calls `label`, `div`, `button` and
`use_state`. Pret finds the names a function reads from outside, its globals and closure variables, and wraps the
function in a factory that receives them as arguments. The factory name holds the ID of the kernel's CBOR stream
described [below](#what-a-notebook-output-contains), `45c6b961` here, and the name of the function:

```python
def pret_factory_45c6b961_label_0(greeting):
    def label(count):
        return f"{greeting}, clicked {count} times"

    return label
```

The factory is translated once, and the captured values are sent next to it. The browser then recreates the
function by calling the factory with these values. Since `Counter` captures `label`, which is itself a function,
`label` is sent the same way as one of the values of `Counter`'s factory, and so on recursively, for functions,
classes and objects alike.

These values are encoded with [CBOR](https://cbor.io/), a binary format close to JSON, extended with tags for the
values JSON cannot describe. For instance, `label` becomes the tag 4000 holding its factory name and captured
values, 72 bytes in all:

```text
4000(["pret_factory_45c6b961_label_0", ["Hello"]])
```

| Tag | Python value | Recreated in the browser by |
| --- | --- | --- |
| 4000 | A function, as its factory name and captured values | calling the factory with the values |
| 4001 | A class, as its name, bases, attributes and methods | Transcrypt's class constructor |
| 4002 | An object with `__reduce__`, as a function and its arguments | calling the function, for instance `rebuild_dataframe` for a DataFrame |
| 4003 | Another object, as its class and attributes | creating an instance and setting its attributes |

!!! info "Why CBOR"

    Pickle can only be read by Python, and JSON has no way to tag a value or to send bytes. CBOR also keeps shared
    references: a Store used by two components is encoded once and remains a single object in the browser, where
    both components see the same edits. Finally, bytes travel as they are, which matters for a Store without `sync`,
    whose DataFrame rows are sent inside the CBOR data as Arrow bytes.

## What a notebook output contains

Displaying a component appends its values to a CBOR stream shared by the whole kernel, and its factories to a
single JavaScript bundle. The notebook output only names that stream and the position of the component in it:

```json
{"application/vnd.pret+json": {"detach": false, "view_data": {"marshaler_id": "45c6b961…", "chunk_idx": 3}}}
```

When the page displays this output, it asks the kernel for the bytes it has not received yet, along with the
bundle, then decodes the component. Functions whose code was already sent, such as the functions of a cell run
again, reuse their factory, so displaying a component again mostly sends new values.

This is also why an output saved in a notebook cannot be displayed after the kernel restarts: its stream no longer
exists, and the cell has to be run again. An app started with `pret.run` serves the same data and bundle from its
web server.

## Calling back into Python (`@server_only`)

Some functions have to run in Python, because they read files, query a database or use a library that has no
browser version. Decorating them with `server_only`, as shown in
[Interacting with the server](../tutorials/interacting-with-the-server.md), keeps them in Python, and the
browser receives a stub instead. When the function is called from the browser, the stub sends the call to the
kernel as a message:

```json
{"function_id": "…", "args": [42], "kwargs": {}, "callback_id": "…"}
```

The call returns a promise, which resolves with the result once the kernel has run the Python function. A
`server_only` function therefore becomes asynchronous in the browser, even when it is a plain `def` in Python:
browser code has to `await` its result. Since a component cannot wait while it renders, the call goes in an event
handler or an effect, which stores the result in a state:

```python
@server_only
def read_title(path):
    return Path(path).read_text()


@component
def Title():
    title, set_title = use_state("Loading…")

    async def load():
        set_title(await read_title("title.txt"))

    use_effect(load, [])
    return div(title)
```

Results go through JSON, except bytes, which are sent as binary buffers of the same message: a PDF returned by a
server function, or a page of DataFrame rows as Arrow data, arrives in the browser as a `Uint8Array` without being
converted to text.

The DataFrames of a connected Store use these calls for their queries, see
[How widgets query DataFrames](dataframe-queries.md). Store edits have their own messages, see
[What happens when you edit a Store](store-edits.md).
