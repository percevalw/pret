# Changelog

## v0.7.0-beta.1 (2026-10-08)

### DataFrames

- `pret.DataFrame` values can be stored in a Store. Records, pandas and Arrow tables are copied to SQLite and editable, Ibis and Spark queries stay in their source and are read only (`apply_ibis_filter`, custom `DataFrameProvider`s).
- Browsers receive pages of rows (`dataframe_sync="paged"`, the default) or a complete copy (`dataframe_sync="full"` with `dataframe_max_bytes`). Edits are checked row by row, an edit based on a row another user changed since is refused as a conflict.
- `use_dataframe_view` subscribes a component to a filtered and sorted view of a DataFrame.

### Saving Stores

- `save_store` and `load_store` write and reopen a Store, its DataFrames and its edit history as one SQLite file. Its tables and views are readable with plain SQLite, ordinary values are a JSON document in `_pret_state`.
- `load_store_snapshot` returns plain Python values, DataFrames as in memory copies of their rows.
- Store files of earlier releases are converted the first time they are opened, which needs `pip install 'pret[legacy]'`, the original is kept as `name.legacy`.

### Store values and collaboration

- Store values no longer use Yjs or pycrdt. They are a JSON tree changed by small operations, `set`, `delete` and `insert` on paths. Python decides every edit: concurrent list insertions are all kept, an edit under a value removed by a newer commit is refused, the last write wins on the same key.
- `pret-pycrdt` is now the optional `legacy` extra and `yjs` is no longer bundled.
- Undo and redo are kept per browser page and skip values another page changed since, see `use_store_history`. Awaited Python callbacks of a page join its history.
- A failed save restores the previous values and keeps the undo history.
- A captured value, eg `todo = store["todos"][2]`, keeps following its item when items are inserted or removed before it, in Python and in the browser.

### Breaking changes

- `subscribe(store, callback)` now calls `callback(ops, change)`, where `ops` are the JSON operations under the watched value, eg `{"op": "set", "path": [0, "done"], "value": True}`. It returns a function that unsubscribes.
- Store values must be JSON values. Integers must be within ±(2^53 - 1), NaN and infinities are refused, tuples become lists, and browser `BigInt` values become numbers.
- In compiled browser code, `key in store_mapping` is now false for missing keys, `list.push` returns the new length and `splice` returns the removed items.
- ipykernel 7.0 to 7.3 are excluded, they can leave browser requests unread so pages stop following their kernel ([ipykernel#1529](https://github.com/ipython/ipykernel/pull/1529)).

### Other changes

- `python -m pret.testing run --suite python|client|jupyter|all` runs Python, browser and JupyterLab tests, `--coverage` merges Python and TypeScript coverage. Extensions such as Metanno reuse it through `[tool.pret.testing]`.
- JupyterLab and standalone bundles are built in production mode, standalone apps share pret with extensions.
- Fix `sorted` and `list.sort` in transpiled code, which are now stable and compare tuples item by item.
- Fix an async `use_effect` leaving its Promise as React cleanup.
- Fix a "File Save Error" dialog when a notebook is closed just after a Pret view renders.
- Server functions returning bytes send them as binary, received as a `Uint8Array`.
- Bundles are serialized safely, views rendered again reuse their browser code, Joy views follow the JupyterLab theme.
- `cbor2` is pinned below 5.8, which fails to decode Pret bundles.
- With ipykernel 7.4 and JupyterLab 4.6, Pret requests are no longer dropped once the subshell of the request that loaded a Store is deleted, eg after a Metanno view is reloaded.

## v0.6.4 (2026-05-10)

- Fix z-index issue to make Joy popup lists appear in full-page mode, while still keeping jupyter main body and modals hidden

## v0.6.3 (2026-05-08)

- Avoid Transcrypt's silent failures by calling it as a subprocess instead of importing it.
- Synchronized stores should now be more effective at blocking writes while disconnected or while sync is blocked and roll back unsaved pending changes on disconnection.
- Such disconnections now trigger a state synchronization upon connection recovery.
- In Jupyter, we now better detect app disconnections by listening to global jupyter service connection failure signals.
- In Jupyter, we should better recover/attach comms on disconnected or already running notebooks.
- The `use_connection_status` hook is now notified (`state_write_rejection_count` and `last_state_write_rejection` fields) when the user tries to edit a synchronized state in a disconnected app.
- `@component` can now be used as a decorator factory (`@component()`) and accepts `memo=True` to wrap the generated React element in `React.memo`.
- Standalone bundles now store serialized data and generated code separately, and Jupyter bundle transfers now send binary bundle chunks with optional gzip compression : this should make the initial loading of components faster and more reliable in bad network scenarios.
- Full page mode now hides the underlying jupyter UI elements, preventing user actions from editing the notebook

## v0.6.2 (2026-02-28)

- Fix remote function call from the backend

## v0.6.1 (2026-02-27)

- Improved connection status detection in Jupyter
- In Jupyter, when using "Open in a new tab" command, we now better preserver the original URL (as Jupyter rewrites it for various reasons)

## v0.6.0 (2026-02-26)

- Added a new `load_store_snapshot` function to load a Python snapshot of the store state without subscribing to updates or risking mutations. This can be useful for instance in Metanno to load the annotations in a read-only pure Python format for export or processing purposes.
- We now warn users when they call `create_store` with initial data and a file already exists at the specified path, to prevent accidental overwriting of existing data.
- `create_store` now also accepts data as a callable that returns the initial data, to support lazy initialization of the store state.
- Jupyter outputs now send only `marshaler_id`/`chunk_idx` and fetch bundles on demand. This should reduce the notebooks file size drastically for apps containing large amounts of data, and speed up the rendering of outputs.
- New "Open in a new browser tab" command on Pret widgets in jupyter to create a new full-viewport view that overlays and hides jupyter simple presentation mode.
- New "Restart app" button when widget is detected as stale or kernel is down upon widget mounting
- Rendering a pret widget now autosaves the notebook to limit the kernel-notebook desynchronization issues
- All tutorials are now runnable notebooks, rendered identically as documentation pages
- New `transact` function to group mutations as a single transaction.

## v0.5.2 (2025-12-08)

- Allow to specify dependencies when using `use_effect` as a decorator
- Transpiled `next` should now support a default parameter

## v0.5.1 (2025-11-26)

- Fix `pret update-jupyter-config` to support venv and envs with no existing configs
- Better support for lambda functions marshaling
- New useImperativeHandle hook
- Support for remote refs, ie `useRef` called on the server/kernel, to interact with client-side components or assemble them without a controller component.
- Introduced to concept of "Widget"
- New option host in `run`, e.g., `run(app, ..., host='0.0.0.0')`

## v0.4.1 (2025-08-21)

- Added script to update jupyter config to let it know where to find the custom environment data files (including our js artifacts)
- Added routing support, and moved base assets to "/assets"
- Added a `names` option to `pret stub` to only generate stubs for specific js objects

## v0.4.0 (2025-08-19)

- Drop valtio in favor of a YJS based shared state management, with a speedup up to 5x for mutations in large arrays.
- Fixed prepack command that didn't collect used js modules

## v0.3.0 (2025-07-01)

- Add file system persistence for stores, using concatenations of yjs/pycrdt update binaries. Using file watchers, this also enables collaboration between multiple users using different servers/kernels.
- Renamed `proxy(..., remote_sync=...)` to `create_store(..., sync=...)` to better reflect the purpose of the function.
- Fixed front end to support large binary updates

## v0.2.0 (2025-06-10)

- **Major change**: I dropped pyodide and replaced it with Python → JavaScript transpilation (using Transcrypt) on the server. This comes with a few caveats such as the lack of support for some Python constructs and modules (to be documented), but brings a lot of benefits:

    - **Runtime performance**: the runtime is now much faster, as it runs in JavaScript the browser and does not require a converting Python objects to JavaScript objects and back.
    - **First-time load**: the first-time load is now much faster, as we don't require to load the Pyodide runtime and the Python standard library in the browser.
    - **Portability**: pret can now run without Internet access, as it does not require to load the Pyodide.
    - **Error handling**: errors (and any thrown object) are now bubbled outside Python and can be handled in JavaScript, which allows for better error handling, as well as throwing Promises to support React Suspense.

- Pret _tracked_ proxies are now marshaled as valtio and proxies in the browser: it is no longer possible to mutate the state using the output of `use_store_snapshot`.
- Dropped old custom valtio-like python state management, and use (pret-)pycrdt instead
- Added dependency to [pret-pycrdt](https://github.com/percevalw/pycrdt), fork of [pycrdt](https://github.com/y-crdt/pycrdt) to support identity preservation and therefore pickling, required for our marshaling mechanism
- Improved notebook-kernel pret manager synchronization and resynchronization

## v0.1.0 (2025-03-20): Initial release :tada:

Pret is a library for building full-stack reactive user interfaces in Python, using React as a rendering engine.

### Installation

```bash
pip install pret pret-joy
```

### Features

- **Python, only Python**: pret is written in Python: you can write your both your UI and server actions Python. No need to learn a new language, or to use a transpiler.
- **Client-side rendering**: unlike other Python UI frameworks, pret runs primarily in the browser. This enables a fast response time to user actions (like hover events), and a better user experience under degraded network conditions.
- **Built on React**: pret uses React as a rendering engine, and benefits from its ecosystem.
- **Reactive**: unlike other solutions like ipywidgets, pret is reactive. Only the parts of the UI that need to be updated are re-rendered.
- **State management**: in addition to React's local state management (i.e. `use_state`), pret provides a global and modular state management solution that is synchronized both between components, and between the client and the server.
- **Modular**: pret is designed to be modular. You can easily create your own components, and reuse them in other pret-based projects.
- **Integrated with Jupyter**: pret components can be used in Jupyter notebooks, as well as in standalone web applications.
- **Remote execution**: pret can call and use the result of Python functions on the server from the browser
