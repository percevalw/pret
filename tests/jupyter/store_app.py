"""
Expose real Store resources and Python callbacks to notebook browser tests
"""

import asyncio

from pret import DataFrame, component, create_store, server_only, use_store_snapshot
from pret.marshal import js
from pret.react import div, p
from pret.store import transact


def setup(mode="full", path=True, max_bytes=1_000_000):
    """
    Create the notebook Store used by editing and peer synchronization scenarios
    """
    global state, todo
    state = create_store(
        {
            "reviewed": False,
            "todo": {"milk": False, "report": False},
            "items": [{"text": "first"}],
            "todos": DataFrame(
                [{"id": "todo", "label": "old", "comment": "", "score": 0}]
                + [{"id": f"other{idx}", "label": "other", "comment": "", "score": 0} for idx in range(599)]
            ),
            "tags": DataFrame([{"id": "tag", "todo_id": "todo", "label": "old"}]),
        },
        sync=path,
        dataframe_sync=mode,
        dataframe_max_bytes=max_bytes,
    )
    # A nested value captured on its own by browser code, like widgets keeping one list of a Store
    todo = state["todo"]
    return state


@server_only
def peer(values, identity="todo"):
    """
    Apply an independent Python table edit and return its revision for browser synchronization
    """
    with state.doc.edit_transaction(origin="peer"):
        state["todos"].update(identity={"id": identity}, values=values)
    return state.revision


@server_only
async def callback(label):
    """
    Update ordinary state and a table after an await using the calling page history
    """
    await asyncio.sleep(0)
    with transact(state):
        state["reviewed"] = False
        state["todo"]["report"] = True
        state["todos"].update(identity={"id": "todo"}, values={"label": label, "score": 3})
    return state.revision


@server_only
def add_table():
    """
    Materialize a table whose ordinary Store reference is sent through the live comm
    """
    state["added"] = DataFrame([{"label": "new table"}])
    return state.revision


@server_only
def fail_next_commit():
    """
    Reject one SQLite commit so the browser can check that a failed commit changes nothing
    """
    attempts = []

    def reject():
        """
        Reject the first commit only, the following edits are saved again
        """
        attempts.append(True)
        return len(attempts) == 1

    state.doc.database.connection.set_commit_hook(reject)


@server_only
def backend_data(revision=None):
    """
    Return transport metadata so tests can distinguish row changes from full table downloads
    """
    data = state.doc.browser_data(revision).data
    return {"revision": data["revision"], "tables": data["tables"], "commits": data["commits"]}


@component
def StoreApp(store):
    """
    Render a PRET Store and expose its decoded client resources for browser assertions
    """
    js.fixtureStore = store
    js.fixtureTodo = todo
    js.fixturePeer = peer
    js.fixtureCallback = callback
    js.fixtureAddTable = add_table
    js.fixtureFailCommit = fail_next_commit
    js.fixtureBackendData = backend_data
    snapshot = use_store_snapshot(store)
    return div(
        p("reviewed" if snapshot["reviewed"] else "unreviewed"),
        p("milk" if snapshot["todo"]["milk"] else "no milk", id="milk"),
        p("report" if snapshot["todo"]["report"] else "no report", id="report"),
        id="store-app",
    )
