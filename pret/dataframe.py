"""
Public DataFrame interface, derived views and browser serialization
"""

from __future__ import annotations

import inspect
import uuid

import pyarrow as pa

from pret.arrow import calculate_arrow_size, serialize_arrow_as_bytes
from pret.dataframe_external import IbisDataFrameProvider
from pret.dataframe_provider import (
    DataFrameDescription,
    DataFrameProvider,
    EditOperation,
    EditResult,
)
from pret.dataframe_sqlite import ArrowSnapshotProvider, SQLiteDataFrameProvider
from pret.manager import BinaryPayload, server_only
from pret.marshal import marshal_as


@marshal_as(
    js="""
return function rebuild_dataframe(resource_id, binding_id, content, owner_id, transactional) {
    if (content.bytes) return window.storeLib.createOfflineDataFrame(resource_id, binding_id, content.bytes);
    const dataframe = window.storeLib.createClientDataFrame(
        {__pret_dataframe__: true, resource_id, binding_id, localCopy: content.local === true, ...content});
    return owner_id ? window.storeLib.bindDataFrameResource(dataframe, owner_id, transactional) : dataframe;
}
""",
)
def rebuild_dataframe(*args):
    """
    Called by the browser loader to rebuild a serialized DataFrame
    """
    return DataFrame(resource_id=args[0])


class DefaultColumnsView(DataFrameProvider):
    """
    Add missing columns requested by DataFrame.with_columns.

    For example, with_columns(done=False) adds a done column to query results.
    Existing columns keep their values. Saving copies these added values into the
    saved table, while the original source remains unchanged.
    """

    def __init__(self, parent, defaults):
        self.parent = parent
        self.defaults = defaults

    def add_columns(self, table):
        """
        Add missing columns to an Arrow table returned by query or materialize
        """
        table = as_arrow(table)
        for name, value in self.defaults.items():
            if name not in table.schema.names:
                if callable(value):
                    column_type = pa.scalar(value()).type
                    column = pa.array([value() for _ in range(table.num_rows)], type=column_type)
                else:
                    column = pa.array([value] * table.num_rows, type=pa.scalar(value).type)
                table = table.append_column(name, column)
        return table

    async def describe(self):
        """
        Include the added columns in the description used to configure widgets
        """
        description = await self.parent.describe()
        names = {column["name"] for column in description["columns"]}
        description["columns"] = [*description["columns"]]
        for name, value in self.defaults.items():
            if name not in names:
                sample = value() if callable(value) else value
                description["columns"].append(
                    {
                        "name": name,
                        "type": str(pa.scalar(sample).type),
                        "nullable": sample is None,
                        "source_filter": False,
                    }
                )
        description["capabilities"] = {
            **description["capabilities"],
            "edit": False,
            "changes": False,
        }
        return description

    async def query(self, options=None):
        """
        Fetch a page from the parent DataFrame and add the requested default columns
        """
        options = dict(options or {})
        columns = options.pop("columns", None)
        result = dict(await self.parent.call_provider("query", options))
        table = self.add_columns(result["table"])
        if columns is not None:
            internal = [name for name in table.column_names if name == "__pret_row_id" and name not in columns]
            table = table.select([*internal, *columns])
        result["table"] = table
        return result

    def materialize(self, bounds=None):
        """
        Fetch every parent row and add the default columns for save_store
        """
        return self.add_columns(self.parent.materialize(bounds))


def as_arrow(value) -> pa.Table:
    """
    Convert rows or a pandas table to Arrow for Store storage and browser responses
    """
    if isinstance(value, pa.Table):
        return value
    if isinstance(value, pa.RecordBatch):
        return pa.Table.from_batches([value])
    if hasattr(value, "to_arrow"):
        table = value.to_arrow()
        if isinstance(table, pa.Table):
            return table
    if hasattr(value, "columns") and hasattr(value, "__dataframe__"):
        return pa.Table.from_pandas(value, preserve_index=False)
    if isinstance(value, dict):
        return pa.table(value)
    return pa.Table.from_pylist(list(value))


class DataFrame:
    """
    Tabular data that can be placed in a PRET Store and displayed by widgets.

    Create one from rows, an Arrow or pandas table, or a Spark or Ibis expression.
    The provider fetches rows from that source and applies edits when supported.
    Adding an Arrow or pandas DataFrame to a Store copies its rows into the
    SQLite database of that Store. Spark and Ibis expressions remain queries against their source
    until explicitly collected, for example by save_store.

    Parameters
    ----------
    records_or_arrow
        Rows, a column mapping, an Arrow or pandas table, or a Spark or Ibis expression
    provider
        DataFrameProvider subclass used instead of records_or_arrow
    resource_id
        Internal table ID, supplied by PRET when reopening or copying a Store

    Attributes
    ----------
    provider
        Object that reads and optionally edits the underlying data
    store_doc
        Containing Store document, or None before the DataFrame is added to a Store
    """

    def __init__(self, records_or_arrow=None, *, provider: DataFrameProvider | None = None, resource_id=None):
        if records_or_arrow is not None and provider is not None:
            raise ValueError("Pass records_or_arrow or provider, not both")
        if provider is None and records_or_arrow is not None:
            module = type(records_or_arrow).__module__
            if module.startswith(("pyspark.sql", "ibis.expr")):
                provider = IbisDataFrameProvider(records_or_arrow)
            else:
                provider = ArrowSnapshotProvider(as_arrow(records_or_arrow))
        if provider is None and resource_id is None:
            raise ValueError("DataFrame requires records_or_arrow or provider")
        self.provider = provider
        self.store_doc = None
        self.resource_id = resource_id or uuid.uuid4().hex
        # Keep the table ID when saving, but give each Python DataFrame instance a new ID for its browser methods
        self.binding_id = uuid.uuid4().hex

    async def call_provider(self, method, *args):
        value = getattr(self.provider, method)(*args)
        return await value if inspect.isawaitable(value) else value

    async def describe(self) -> DataFrameDescription:
        """
        Return column types, row count and supported operations so widgets can configure their fields
        """
        return await self.call_provider("describe")

    async def query(self, options=None):
        """
        Fetch a page of rows for tables, forms and annotation widgets.

        The provider filters and sorts the full source before selecting the requested
        page. The response includes Arrow bytes and counts used by the table scrollbar.

        Parameters
        ----------
        options
            Filters, sort columns, columns to return, offset and limit, with optional row ID lookups

        Returns
        -------
        BinaryPayload
            Arrow bytes with the filtered row count, Store revision number and any requested row positions
        """
        result = dict(await self.call_provider("query", options or {}))
        table = as_arrow(result.pop("table"))
        output = serialize_arrow_as_bytes(table)
        return BinaryPayload(
            {**result, "resource_id": self.resource_id, "row_count": table.num_rows, "byte_count": len(output)},
            [output],
        )

    async def apply_edit(self, edit: EditOperation) -> EditResult:
        """
        Apply a widget edit through the Store transaction, or through the source when it handles its own edits
        """
        if self.store_doc is not None and isinstance(self.provider, SQLiteDataFrameProvider):
            result = self.store_doc.dataframe_edit(self.resource_id, edit)
            return await result if inspect.isawaitable(result) else result
        return await self.call_provider("apply_edit", edit)

    async def get_changes_after_revision(self, revision):
        """
        Tell a widget whether its displayed rows need to be fetched again
        """
        current = (
            self.provider.database.revision
            if isinstance(self.provider, SQLiteDataFrameProvider)
            else (await self.describe()).get("revision", revision)
        )
        return {"revision": current, "refresh": current > revision}

    def materialize(self, bounds=None) -> pa.Table:
        """
        Fetch all rows as an Arrow table for saving or standalone browser export.

        For Spark or Ibis sources this runs the query and collects its full result.
        Optional max_rows and max_bytes entries in bounds limit the exported data.
        """
        value = self.provider.materialize(bounds)
        if inspect.isawaitable(value):
            if inspect.iscoroutine(value):
                value.close()
            raise TypeError("save_store requires a synchronous provider materialization")
        table = as_arrow(value)
        bounds = bounds or {}
        max_rows = bounds.get("max_rows")
        if max_rows is not None and table.num_rows > max_rows:
            raise ValueError(f"DataFrame exceeds the save bound of {max_rows} rows")
        max_bytes = bounds.get("max_bytes")
        if max_bytes is not None and calculate_arrow_size(table) > max_bytes:
            raise ValueError(f"DataFrame exceeds the save bound of {max_bytes} bytes")
        return table

    def with_columns(self, **defaults):
        """
        Return a DataFrame with missing columns filled from constants or a function called for each row
        """
        description = self.provider.describe()
        if not inspect.isawaitable(description):
            names = {column["name"] for column in description["columns"]}
            if set(defaults) <= names:
                return self
        elif inspect.iscoroutine(description):
            description.close()
        return DataFrame(provider=DefaultColumnsView(self, defaults))

    def submit_edit(self, operation):
        """
        Save a DataFrame.insert, update or delete call with the latest revision number as its base
        """
        describe = self.provider.describe()
        if inspect.isawaitable(describe):
            if inspect.iscoroutine(describe):
                describe.close()
            raise TypeError("Python edit requires a synchronous DataFrame provider")
        edit = {
            "base_revision": int(describe.get("revision", 0)),
            **operation,
        }
        # Store tables save the edit in a Store transaction, custom editable providers apply it themselves
        return (
            self.store_doc.dataframe_edit(self.resource_id, edit)
            if self.store_doc is not None and isinstance(self.provider, SQLiteDataFrameProvider)
            else self.provider.apply_edit(edit)
        )

    def insert(self, values):
        """
        Add one row from a mapping of column names to values
        """
        return self.submit_edit({"kind": "insert", "values": dict(values)})

    def update(self, *, identity, values):
        """
        Change one row matching the identity column values, for example identity=dict(id=3)
        """
        return self.submit_edit({"kind": "update", "identity": dict(identity), "values": dict(values)})

    def delete(self, *, identity):
        """
        Remove one row matching the identity column values, for example identity=dict(id=3)
        """
        return self.submit_edit({"kind": "delete", "identity": dict(identity)})

    def __reduce__(self):
        """
        Send this DataFrame to a browser using the connection mode of its Store.

        Connected Stores send Python methods for queries and edits, or only a flag for tables copied
        in full to the browser. Browser only Stores send the Arrow rows.
        """
        provider = self.provider
        owner = self.store_doc
        if owner is None:
            raise TypeError("pret.DataFrame must be a PRET Store value, use DataWidgetFactory or create_store")
        synchronized = owner.sync_id is not None
        if not synchronized:
            # Read Store tables when rendering so the page includes Python edits made since Store creation
            content = {"bytes": serialize_arrow_as_bytes(self.materialize())}
        elif owner.dataframe_sync == "full" and isinstance(provider, SQLiteDataFrameProvider):
            content = {"local": True}
        else:
            prefix = f"dataframe:{self.resource_id}:{self.binding_id}"
            content = {
                name: server_only(getattr(self, name), f"{prefix}:{name}")
                for name in ("describe", "query", "apply_edit", "get_changes_after_revision")
            }
        return rebuild_dataframe, (
            self.resource_id,
            self.binding_id,
            content,
            owner.origin,
            synchronized and isinstance(provider, SQLiteDataFrameProvider),
        )
