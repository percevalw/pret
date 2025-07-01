# v0.3.0 (2025-07-01)

- Add file system persistence for stores, using concatenations of yjs/pycrdt update binaries. Using file watchers, this also enables collaboration between multiple users using different servers/kernels.
- Renamed `proxy(..., remote_sync=...)` to `create_store(..., sync=...)` to better reflect the purpose of the function.
- Fixed front end to support large binary updates

# v0.2.0 (2025-06-10)

- **Major change**: I dropped pyodide and replaced it with Python → JavaScript transpilation (using Transcrypt) on the server. This comes with a few caveats such as the lack of support for some Python constructs and modules (to be documented), but brings a lot of benefits:

    - **Runtime performance**: the runtime is now much faster, as it runs in JavaScript the browser and does not require a converting Python objects to JavaScript objects and back.
    - **First-time load**: the first-time load is now much faster, as we don't require to load the Pyodide runtime and the Python standard library in the browser.
    - **Portability**: pret can now run without Internet access, as it does not require to load the Pyodide.
    - **Error handling**: errors (and any thrown object) are now bubbled outside Python and can be handled in JavaScript, which allows for better error handling, as well as throwing Promises to support React Suspense.

- Pret _tracked_ proxies are now marshaled as valtio and proxies in the browser: it is no longer possible to mutate the state using the output of `use_store_snapshot`.
- Dropped old custom valtio-like python state management, and use (pret-)pycrdt instead
- Added dependency to [pret-pycrdt](https://github.com/percevalw/pycrdt), fork of [pycrdt](https://github.com/y-crdt/pycrdt) to support identity preservation and therefore pickling, required for our marshaling mechanism
- Improved notebook-kernel pret manager synchronization and resynchronization

# v0.1.0 (2025-03-20): Initial release :tada:

Pret is a library for building full-stack reactive user interfaces in Python, using React as a rendering engine.

## Installation

```bash
pip install pret pret-joy  --user
```

## Features

- **Python, only Python**: pret is written in Python: you can write your both your UI and server actions Python. No need to learn a new language, or to use a transpiler.
- **Client-side rendering**: unlike other Python UI frameworks, pret runs primarily in the browser. This enables a fast response time to user actions (like hover events), and a better user experience under degraded network conditions.
- **Built on React**: pret uses React as a rendering engine, and benefits from its ecosystem.
- **Reactive**: unlike other solutions like ipywidgets, pret is reactive. Only the parts of the UI that need to be updated are re-rendered.
- **State management**: in addition to React's local state management (i.e. `use_state`), pret provides a global and modular state management solution that is synchronized both between components, and between the client and the server.
- **Modular**: pret is designed to be modular. You can easily create your own components, and reuse them in other pret-based projects.
- **Integrated with Jupyter**: pret components can be used in Jupyter notebooks, as well as in standalone web applications.
- **Remote execution**: pret can call and use the result of Python functions on the server from the browser
