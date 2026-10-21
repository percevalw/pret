"""
DataFrame provider that queries external sources through Ibis.

Used by DataFrame when given a Spark or Ibis expression, whether the application
runs in a notebook kernel or a standalone server. Filters and sorting run on the
source, and only the requested page is collected into Arrow for the browser.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING
from uuid import uuid4
from weakref import finalize

import pyarrow as pa

from pret.arrow import calculate_arrow_size
from pret.dataframe_provider import DataFrameDescription, DataFrameProvider, DataFrameQueryResult

if TYPE_CHECKING:
    from ibis.expr.types import Table as IbisTable
    from pyspark.sql import DataFrame as SparkDataFrame


def coerce_ibis_literal(dtype, value, operator):
    """
    Validate one JSON literal against an expression column type.

    Parameters
    ----------
    dtype
        Ibis column type from the source schema
    value
        JSON literal supplied by the browser
    operator
        Predicate operator that will consume the value

    Returns
    -------
    Any
        Typed value passed to the source expression
    """
    if operator == "in":
        return [coerce_ibis_literal(dtype, item, "eq") for item in value]
    if value is None:
        raise ValueError("null literals require is_null or is_not_null")
    if dtype.is_array() or dtype.is_struct() or dtype.is_map() or dtype.is_binary():
        raise ValueError(f"source filtering is unavailable for {dtype}")
    if operator in {"icontains", "contains", "starts_with", "ends_with"} and not dtype.is_string():
        raise ValueError(f"{operator} requires a string column")
    if dtype.is_boolean():
        if not isinstance(value, bool):
            raise ValueError("boolean filter value must be true or false")
        return value
    if dtype.is_integer():
        if isinstance(value, str) and value.lstrip("-").isdigit():
            return int(value)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("integer filter value must be an integer")
        return value
    if dtype.is_floating():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("floating filter value must be numeric")
        return value
    if dtype.is_decimal():
        if not isinstance(value, (int, float, str)) or isinstance(value, bool):
            raise ValueError("decimal filter value must be numeric")
        try:
            return Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("decimal filter value must be numeric") from exc
    if dtype.is_date():
        if not isinstance(value, str):
            raise ValueError("date filter value must use ISO format")
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date filter value must use ISO format") from exc
    if dtype.is_timestamp():
        if not isinstance(value, str):
            raise ValueError("timestamp filter value must use ISO format")
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("timestamp filter value must use ISO format") from exc
    if dtype.is_string() and not isinstance(value, str):
        raise ValueError("string filter value must be a string")
    return value


def compile_predicate(expression, node):
    """
    Convert widget predicates to typed Ibis expressions for apply_ibis_filter
    """
    if not isinstance(node, dict):
        raise ValueError("Source filter must be an object")
    kind = node.get("kind")
    if kind in {"and", "or"}:
        children = node.get("children")
        if not isinstance(children, list) or not children:
            raise ValueError(f"Source {kind} filter requires children")
        predicates = [compile_predicate(expression, child) for child in children]
        predicate = predicates[0]
        for child in predicates[1:]:
            predicate = predicate & child if kind == "and" else predicate | child
        return predicate
    if kind == "not":
        return ~compile_predicate(expression, node.get("child"))
    if kind != "predicate":
        raise ValueError(f"Unsupported source filter kind {kind!r}")
    column_name = node.get("column")
    if column_name not in expression.columns:
        raise ValueError(f"Unknown source filter column {column_name!r}")
    column = expression[column_name]
    operator = node.get("operator")
    value = node.get("value")
    operations = {
        "eq": lambda: column == value,
        "ne": lambda: column != value,
        "lt": lambda: column < value,
        "lte": lambda: column <= value,
        "gt": lambda: column > value,
        "gte": lambda: column >= value,
        "icontains": lambda: column.lower().contains(value.lower()),
        "contains": lambda: column.contains(value),
        "starts_with": lambda: column.startswith(value),
        "ends_with": lambda: column.endswith(value),
        "in": lambda: column.isin(value),
        "is_null": lambda: column.isnull(),
        "is_not_null": lambda: ~column.isnull(),
    }
    if operator not in operations:
        raise ValueError(f"Unsupported source filter operator {operator!r}")
    if operator not in {"is_null", "is_not_null"}:
        if "value" not in node:
            raise ValueError(f"Source {operator} filter requires a value")
        if operator == "in" and not isinstance(value, list):
            raise ValueError("Source in filter value must be a list")
        value = coerce_ibis_literal(column.type(), value, operator)
    return operations[operator]()


def apply_ibis_filter(expression, node, get_related_table=None):
    """
    Apply widget filters and conjunctive relation filters before source counting and paging.

    get_related_table maps a DataFrame resource_id to its lazy source table,
    an Ibis Table object. For a relation filter, restrict that
    related table first and keep calling-table rows whose key occurs in it.
    The semi join keeps each calling-table row once even if related keys repeat.
    """
    if node is None:
        return expression
    if not isinstance(node, dict):
        raise ValueError("Source filter must be an object")
    if node.get("kind") == "and":
        children = node.get("children")
        if not isinstance(children, list) or not children:
            raise ValueError("Source and filter requires children")
        for child in children:
            expression = apply_ibis_filter(expression, child, get_related_table)
        return expression
    if node.get("kind") == "relation":
        if get_related_table is None:
            raise ValueError("Source relation filters require get_related_table to look up the related DataFrame")
        parent = get_related_table(node["resource_id"])
        parent = apply_ibis_filter(parent, node.get("filter"), get_related_table)
        # Give self relations a separate expression before joining their parent keys
        parent = parent.view()
        parent = parent.select(node["parent_column"])
        return expression.join(parent, expression[node["column"]] == parent[node["parent_column"]], how="semi")
    return expression.filter(compile_predicate(expression, node))


def collect_arrow(expression: IbisTable, *, empty=False) -> pa.Table:
    """
    Collect Ibis rows for provider queries and Store exports without losing exact values
    """
    backend = expression.get_backend()
    if backend.name == "pyspark":
        from pyspark.sql.pandas.types import to_arrow_schema

        # Ibis converts Spark through pandas, rounding nullable integers and dropping timestamp timezones
        backend._run_pre_execute_hooks(expression)
        frame = backend.raw_sql(backend.compile(expression)).select(*expression.columns)
        batches = [] if empty else frame._collect_as_arrow()
        schema = batches[0].schema if batches else to_arrow_schema(frame.schema)
        return pa.Table.from_batches(batches, schema=schema)
    if empty:
        return pa.Table.from_batches([], schema=expression.schema().to_pyarrow())
    return expression.to_pyarrow()


class IbisDataFrameProvider(DataFrameProvider):
    """
    Fetch DataFrame pages by running an Ibis expression on its database.

    DataFrame creates this provider for an Ibis or Spark input. describe reads the schema,
    query collects an unordered preview by default, paging enables ordered source pages.
    materialize collects all rows for save_store.
    The source is read only because its row positions are not lasting row IDs.
    """

    def __init__(
        self, expression: IbisTable | SparkDataFrame, row_limit=500, max_bytes=10 * 1024 * 1024, *, paging=False
    ):
        if type(expression).__module__.startswith("pyspark.sql"):
            import ibis

            session = expression.sparkSession
            # Ibis changes these settings when connecting to the borrowed Spark session
            settings = {
                key: session.conf.get(key)
                for key in (
                    "spark.sql.session.timeZone",
                    "spark.sql.mapKeyDedupPolicy",
                )
            }
            backend = ibis.pyspark.connect(session=session, **settings)
            name = "pret_" + uuid4().hex
            expression.createTempView(name)
            # The temporary view is dropped once no Ibis expression uses the backend, the Spark session stays open
            finalize(backend, session.catalog.dropTempView, name).atexit = False
            expression = backend.table(name)
        self.expression = expression
        self.row_limit = row_limit
        self.max_bytes = max_bytes
        self.paging = paging

    def describe(self) -> DataFrameDescription:
        schema = self.expression.schema()
        columns = []
        for name, dtype in schema.items():
            type_name = str(dtype)
            columns.append(
                {
                    "name": name,
                    "type": type_name,
                    "nullable": dtype.nullable,
                    "normalized": type_name.startswith(("array<", "map<", "struct<")) or type_name == "binary",
                }
            )
        return {
            "columns": columns,
            "row_count": None,
            "row_limit": None if self.paging else self.row_limit,
            "internal_id_col": "__pret_row_id",
            "capabilities": {
                "source_filters": True,
                "edit": False,
                "changes": False,
                "stable_identity": False,
                "query": True,
            },
        }

    def query(self, options=None, get_related_table=None) -> DataFrameQueryResult:
        """
        Filter and sort through Ibis, then collect the page requested by the widget
        """
        import ibis

        options = options or {}
        if options.get("identities") is not None:
            raise ValueError("Live Ibis sources have no stable internal identities, save the Store first")
        offset, limit = options.get("offset", 0), options.get("limit", self.row_limit)
        if not isinstance(offset, int) or not isinstance(limit, int) or offset < 0 or limit < 0:
            raise ValueError("Query offset and limit must be nonnegative integers")
        expression = apply_ibis_filter(self.expression, options.get("filter"), get_related_table)
        distinct = options.get("distinct")
        if distinct is not None:
            expression = expression.select(distinct).distinct()
        paging = options.get("paging", self.paging)
        if not paging and (offset or options.get("sort") or options.get("locate")):
            raise ValueError("Limited queries do not support offsets, sorting or row positions")
        ordering = []
        if paging:
            scalar_columns = [
                name
                for name, dtype in expression.schema().items()
                if not str(dtype).startswith(("array<", "map<", "struct<"))
            ]
            for term in options.get("sort", []):
                if term["column"] not in scalar_columns or term["direction"] not in {"asc", "desc"}:
                    raise ValueError("Source sorting requires a scalar column and asc or desc direction")
                column = expression[term["column"]]
                ordering.append(
                    column.asc(nulls_first=True) if term["direction"] == "asc" else column.desc(nulls_first=False)
                )
            sorted_columns = {term["column"] for term in options.get("sort", [])}
            ordering.extend(
                expression[name].asc(nulls_first=True) for name in scalar_columns if name not in sorted_columns
            )
            if not ordering:
                raise ValueError("Paging a live source requires a scalar ordering column, save the Store first")
        result = {
            "total_count": int(expression.count().execute()) if paging and options.get("include_count", True) else None,
            "revision": 0,
            "offset": offset,
        }
        locate = options.get("locate")
        if locate is not None:
            positioned = expression.select(
                value=expression[locate["column"]], position=ibis.row_number().over(order_by=ordering)
            )
            found = positioned.filter(positioned.value.isin(locate["values"]))
            records = collect_arrow(found.group_by("value").aggregate(position=found.position.min())).to_pylist()
            positions = {row["value"]: row["position"] for row in records}
            result["positions"] = [positions.get(value) for value in locate["values"]]
        page = expression.order_by(*ordering).limit(limit, offset=offset) if paging else expression.limit(limit)
        # Select declared columns to exclude backend pagination helper columns
        columns = options.get("columns", expression.columns) if distinct is None else expression.columns
        result["table"] = collect_arrow(page.select(*columns), empty=limit == 0)
        # Loaded source groups use page positions for local selection, these IDs are not saved
        if "__pret_row_id" not in result["table"].column_names:
            result["table"] = result["table"].append_column(
                "__pret_row_id", pa.array(range(offset, offset + result["table"].num_rows), type=pa.int64())
            )
        if calculate_arrow_size(result["table"]) > self.max_bytes:
            raise ValueError("Source query page exceeds the byte limit, request fewer rows or columns")
        return result

    def materialize(self, bounds=None):
        """
        Collect the Ibis result as Arrow data for save_store or standalone browser export
        """
        bounds = bounds or {}
        max_rows = bounds.get("max_rows")
        expression = self.expression.limit(max_rows + 1) if max_rows is not None else self.expression
        table = collect_arrow(expression)
        if max_rows is not None and table.num_rows > max_rows:
            raise ValueError(f"DataFrame exceeds the save bound of {max_rows} rows")
        return table
