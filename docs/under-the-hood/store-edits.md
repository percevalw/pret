# What happens when you edit a Store

When a widget changes a connected Store, for instance when you tick a checkbox bound to `store["todos"][0]["done"]`
or edit a cell of a DataFrame, the page shows the new value at once and sends the edit to the Python Store, which
saves it. Every other page and notebook showing the same Store then receives the edit a moment later. This page
follows such an edit from the page to Python and back, then explains what happens when the connection drops, when
two people edit the same value, and how undo keeps the edits of others. [Inside a Store file](store-files.ipynb)
describes the SQLite tables mentioned below.

## Who takes part

Three parts are involved in an edit:

- the Python Store, one per notebook kernel or Python process, which holds the current values, decides whether an
  edit is accepted, saves it and keeps the undo history of each page
- the page controller, one per Store in each browser page, which shows edits at once, sends them to Python one at
  a time and applies the changes made elsewhere
- the SQLite database, a file or an in-memory database, where Python saves each accepted edit as one revision

A Store created without `sync` never talks to Python: its edits stay in the page and cannot be undone. The rest of
this page is about connected Stores, created with `sync=True` or a file.

## Two kinds of edits

A Store holds ordinary values, such as mappings, lists, strings and numbers, and DataFrames, and the page describes
an edit of each differently. When you change an ordinary value, the page records operations on paths of the Store's
JSON document: `set` for a mapping key or a list item, and `insert` and `delete` for list items. For example:

```json
{"op": "set", "path": ["data", "todos", 0, "done"], "value": true}
```

A DataFrame cell cannot be named by a path, since the table is not part of the document and can be too large to be
sent to the page anyway. The page names the row by its internal row ID instead, and lists the new values of its
columns:

```json
{"resource_id": "todos-id", "row_identity": 1, "values": {"done": true}}
```

Otherwise both kinds travel together: one widget action can change both, for instance a cell and the label of a
filter, and Python saves them in the same revision. They only differ when someone else edited the Store in the
meantime, as explained [below](#python-checks-and-saves-it).

## The path of an edit

### The page shows the edit at once

Waiting for Python before showing a click would make every widget feel slow. The page therefore applies the edit to
its own copy of the values before Python answers, a technique known as optimistic update. Ordinary values are
changed in the page's copy of the JSON document. DataFrame rows depend on how the page holds them for that Store:

- with `dataframe_sync="paged"`, the default, the page only has the rows it has already fetched, and shows the new
  values over them
- with `dataframe_sync="full"`, the page holds a SQLite copy of the tables and writes the edit to it inside a
  savepoint, so that local filters and sorts take it into account at once. Inserted rows get a temporary negative
  ID until Python assigns the real one

### The page sends one request

The page then sends both kinds of changes to Python as one request:

```json
{
  "request_id": "edit-42",
  "origin": "page-a",
  "kind": "edit",
  "base_revision": 7,
  "ops": [{"op": "set", "path": ["data", "todos", 0, "done"], "value": true}],
  "edits": [{"resource_id": "todos-id", "base_revision": 7, "row_identity": 1, "values": {"done": true}}]
}
```

- `ops` lists the operations on ordinary values, which may only change paths under `"data"`
- `edits` lists the row edits
- `base_revision` is the revision the page showed when the user made the edit
- `request_id` lets the page send the request again if needed
- `origin` identifies the page, whose undo history receives the edit

A page waits for the reply before sending another request, and a second local edit raises until then.

### Python checks and saves it

Other pages or notebooks may have saved revisions since `base_revision`, and the paths of the page operations can
then point to the wrong item. Python therefore moves these operations over the newer revisions, as if the user had
made them afterwards, a technique known as operational transformation:

| Saved since `base_revision` | Page operation | What Python saves |
| --- | --- | --- |
| insert at `["data", "todos", 0]` | insert at `["data", "todos", 0]` | the page item after the saved one, both are kept |
| insert at `["data", "todos", 0]` | set at `["data", "todos", 2, "done"]` | set at `["data", "todos", 3, "done"]` |
| set at `["data", "title"]` | set at `["data", "title"]` | the page value, the last writer wins |
| delete at `["data", "todos", 2]` | set at `["data", "todos", 2, "done"]` | nothing, the request is a conflict |

Python views such as `todo = store["todos"][2]` follow their item in the same way when items are inserted or
deleted before it. DataFrame rows need no such transformation, since their IDs do not change when other rows are
inserted or deleted: Python only checks that no later revision changed the same cell, or inserted or deleted the
row, and rejects the edit otherwise.

When everything applies, Python saves the operations and the row changes as revision 8, in one SQLite transaction,
and replies:

```json
{"request_id": "edit-42", "status": "applied", "revision": 8, "row_identities": [[1]], "can_undo": true, "can_redo": false}
```

The page then replaces its optimistic values with the saved ones. In full mode, it rolls back to its savepoint and
writes the accepted rows in the same step, then maps temporary row IDs to the saved ones, so that the table does not
flash intermediate rows.

### Other pages catch up

Once the revision is saved, Python tells every page showing this Store that it changed:

```json
{"sync_id": "store-id"}
```

Each page then asks for the revisions after the last one it applied. A page holding fetched rows also names their
IDs, and receives their new values along with a summary of the other changes:

```json
{"revision": 9, "commits": [{"revision": 9, "request_id": "edit-43", "origin": "page-b", "kind": "edit",
  "ops": [{"op": "insert", "path": ["data", "todos", 0], "value": {"text": "tea"}}],
  "changes": [{"resource_id": "todos-id", "kind": "update", "offset": 0, "length": 512}],
  "summaries": [{"resource_id": "todos-id", "kind": "update", "columns": ["done"], "count": 1}]}]}
```

- `ops` are applied as they are to the page's copy of the document
- `changes` point to the Arrow data of the changed rows, sent with the message at the given byte offset
- a summary tells a widget to query its rows again when a changed column is part of its filter or sort

In full mode the reply contains every changed row, which the page writes to its copy. If the page itself had a
pending edit and Python accepted it unchanged, the page keeps the values it shows. Otherwise it cancels its
optimistic values and applies the revisions in order.

Another Python process can also save revisions to the same file. Each process checks the file before each change
and every 0.2 seconds while the Store is in use, applies the revisions saved by the others, then notifies its own
pages in the same way. SQLite's lock makes two processes save their revisions one after the other.

## When the edit does not go through

### The reply is lost

Python saves the edit before replying, so a lost reply does not mean a lost edit. The page keeps the edit pending
and sends the same `request_id` again when the connection returns. Python finds the reply it saved for that request
in `_pret_acknowledgments` and returns it, without applying the edit a second time.

In a notebook, all Pret outputs of a session share one connection to the kernel. It opens with the first Pret output
and reconnects on its own as long as an output remains open, including in a hidden tab. When Python cannot be
reached as the page loads, for instance on a static documentation page, the Store shows the values sent with the
page and refuses edits until the connection returns.

### The edit is rejected

When an operation or a row edit conflicts with a newer revision, or is invalid, Python saves nothing and replies
with `"status": "conflict"` or `"failed"`. The page then cancels its optimistic values, both the ordinary values
and the table cells.

### Saving fails

When SQLite itself fails while saving, nothing is saved either: Python restores the values the edit changed by
applying the inverse of its operations, and keeps the page histories. If this restoration fails too, the Store
refuses further edits.

## Undo and redo

Each page undoes its own edits only, without erasing what others changed since. To do so, Python records which
revisions each page made, and every revision saves the inverse of its operations. Undo is sent like any other
request:

```json
{"request_id": "undo-7", "origin": "page-a", "kind": "undo", "base_revision": 9, "ops": [], "edits": []}
```

Python then restores the cells changed by the last edit of that page, except cells another page changed since, even
to the same value. In the same transaction, it applies the inverse operations, transformed over the newer
revisions, and skips the values another page changed since. Redo repeats only what undo restored, and the reply
carries `can_undo` and `can_redo` for the page buttons.

Reloading a page gives it a new origin, so it starts with empty undo and redo lists. A notebook callback awaited by
a page joins that page's history.
