"""
Base class and result types for DataFrame storage and source adapters
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Awaitable, Literal, TypedDict

import pyarrow as pa
from typing_extensions import NotRequired


class DataFrameColumn(TypedDict):
    name: str
    type: str
    nullable: bool
    normalized: NotRequired[bool]
    source_filter: NotRequired[bool]


class DataFrameDescription(TypedDict):
    columns: list[DataFrameColumn]
    row_count: int | None
    row_limit: NotRequired[int | None]
    capabilities: dict[str, bool]
    revision: NotRequired[int]
    internal_id_col: NotRequired[str]


class DataFrameQueryResult(TypedDict):
    """
    Arrow rows and query metadata that DataFrame.query serializes for widgets
    """

    table: pa.Table
    total_count: int | None
    revision: NotRequired[int]
    offset: NotRequired[int]
    positions: NotRequired[list[int | None]]


class EditOperation(TypedDict, total=False):
    """
    Row insertions, deletions or cell changes sent by a widget to DataFrame.apply_edit
    """

    request_id: str
    origin: str
    base_revision: int
    kind: Literal["insert", "update", "delete", "batch"]
    row_identity: int | str
    identity: dict[str, Any]
    column: str
    value: Any
    values: dict[str, Any]
    edits: list[EditOperation]


class EditResult(TypedDict):
    status: Literal["applied", "conflict"]
    revision: int
    request_id: NotRequired[str]
    row_identity: NotRequired[int | str]
    row_identities: NotRequired[list[int]]


class DataFrameProvider(ABC):
    """
    Read a table for DataFrame widgets, with optional saving and editing support.

    Subclasses implement describe and query. These methods can return their result
    directly or asynchronously, DataFrame awaits either form. A query returns an
    Arrow table containing the requested rows, not serialized bytes or SQL text.

    Providers that support save_store also implement synchronous materialize.
    Editable providers implement apply_edit and report edit capability in describe.
    """

    @abstractmethod
    def describe(self) -> DataFrameDescription | Awaitable[DataFrameDescription]:
        """
        Return column types, row counts and supported operations for widget configuration
        """

    @abstractmethod
    def query(self, options: dict[str, Any] | None = None) -> DataFrameQueryResult | Awaitable[DataFrameQueryResult]:
        """
        Filter and sort the source before returning the requested rows as Arrow data

        Parameters
        ----------
        options
            Query fields include filter, sort, columns, offset and limit
            include_count controls whether total_count must be computed
            locate requests positions for supplied column values
            identities requests rows by their internal row IDs

        Returns
        -------
        DataFrameQueryResult or Awaitable of DataFrameQueryResult
            table contains the requested rows, total_count counts matching source
            rows before pagination or is None when the count was not requested
            positions follows the order of values supplied to locate
        """

    def materialize(self, bounds: dict[str, int] | None = None) -> pa.Table:
        """
        Collect the complete source for save_store or standalone widget export

        Parameters
        ----------
        bounds
            Optional max_rows and max_bytes limits on the collected table

        Returns
        -------
        pyarrow.Table
            Source rows to copy into the destination Store
        """
        raise NotImplementedError(
            "This provider cannot be saved, implement synchronous materialize to collect its rows"
        )

    def apply_edit(self, edit: EditOperation) -> EditResult | Awaitable[EditResult]:
        """
        Apply a row mutation for DataFrame.insert, update, delete or a widget edit

        Providers without an override are read only
        """
        raise NotImplementedError("This DataFrame provider is read only")
