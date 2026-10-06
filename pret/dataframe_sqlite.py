"""
Arrow input and SQLite table providers used by DataFrame
"""

from __future__ import annotations

import base64
import datetime
import decimal
import json
import math
import uuid
from typing import TYPE_CHECKING

import pyarrow as pa

from pret.dataframe_provider import DataFrameProvider

if TYPE_CHECKING:
    from pret.store import StoreDatabase

INTERNAL_ROW_ID = "__pret_row_id"


def quote_identifier(name):
    """
    Quote a table or column name for SQL statements built by PRET
    """
    return '"' + name.replace('"', '""') + '"'


def is_list_type(arrow_type):
    return pa.types.is_list(arrow_type) or pa.types.is_large_list(arrow_type) or pa.types.is_fixed_size_list(arrow_type)


def is_binary_type(arrow_type):
    return (
        pa.types.is_binary(arrow_type)
        or pa.types.is_large_binary(arrow_type)
        or pa.types.is_fixed_size_binary(arrow_type)
    )


def sqlite_type(arrow_type):
    """
    Choose the SQLite column type used to store and query an Arrow field.

    Exact numbers and temporal values are stored as text, nested values as JSON text
    after checking that their child types are supported.
    """
    if pa.types.is_uint64(arrow_type) or pa.types.is_decimal(arrow_type):
        return "TEXT"
    if pa.types.is_temporal(arrow_type) and not pa.types.is_interval(arrow_type):
        return "TEXT"
    if pa.types.is_boolean(arrow_type) or pa.types.is_integer(arrow_type):
        return "INTEGER"
    if pa.types.is_floating(arrow_type):
        return "REAL"
    if is_binary_type(arrow_type):
        return "BLOB"
    if pa.types.is_struct(arrow_type):
        for field in arrow_type:
            sqlite_type(field.type)
        return "TEXT"
    if is_list_type(arrow_type):
        sqlite_type(arrow_type.value_type)
        return "TEXT"
    if pa.types.is_map(arrow_type):
        sqlite_type(arrow_type.key_type)
        sqlite_type(arrow_type.item_type)
        return "TEXT"
    if pa.types.is_string(arrow_type) or pa.types.is_large_string(arrow_type) or pa.types.is_null(arrow_type):
        return "TEXT"
    raise TypeError(f"DataFrame storage does not support Arrow type {arrow_type}")


def numeric_key(value):
    """
    Order exact decimal and integer text in PRET queries without rounding
    """
    if value is None:
        return None
    digits = value.replace(".", "").lstrip("-0") or "0"
    key = f"{len(digits):010d}" + digits
    return "0" + key.translate(str.maketrans("0123456789", "9876543210")) if value.startswith("-") else "1" + key


def query_column(name, arrow_type):
    """
    Build the comparison expression used by DataFrame filters and ordering
    """
    name = quote_identifier(name)
    if pa.types.is_decimal(arrow_type) or pa.types.is_uint64(arrow_type) or pa.types.is_duration(arrow_type):
        return f"_pret_numeric_key({name})"
    return name


def physical_arrow_type(arrow_type):
    """
    Read dates and times as integer counts so conversion to Python does not lose precision
    """
    if pa.types.is_temporal(arrow_type):
        return pa.int32() if pa.types.is_date32(arrow_type) or pa.types.is_time32(arrow_type) else pa.int64()
    if pa.types.is_struct(arrow_type):
        return pa.struct([field.with_type(physical_arrow_type(field.type)) for field in arrow_type])
    if pa.types.is_list(arrow_type) or pa.types.is_fixed_size_list(arrow_type):
        size = arrow_type.list_size if pa.types.is_fixed_size_list(arrow_type) else -1
        return pa.list_(arrow_type.value_field.with_type(physical_arrow_type(arrow_type.value_type)), size)
    if pa.types.is_large_list(arrow_type):
        return pa.large_list(arrow_type.value_field.with_type(physical_arrow_type(arrow_type.value_type)))
    if pa.types.is_map(arrow_type):
        return pa.map_(
            arrow_type.key_field.with_type(physical_arrow_type(arrow_type.key_type)),
            arrow_type.item_field.with_type(physical_arrow_type(arrow_type.item_type)),
            keys_sorted=arrow_type.keys_sorted,
        )
    return arrow_type


def encode_value(value, arrow_type, nested=False):
    """
    Convert an Arrow cell value to a SQLite value, encoding nested cells as JSON
    """
    if value is None:
        return None
    if pa.types.is_floating(arrow_type) and math.isnan(value):
        raise ValueError("SQLite cannot preserve NaN separately from null, replace NaN with Arrow null values")
    if nested and pa.types.is_floating(arrow_type) and not math.isfinite(value):
        raise ValueError("Nested DataFrame values cannot contain nonfinite floats")
    if pa.types.is_struct(arrow_type):
        value = {field.name: encode_value(value[field.name], field.type, True) for field in arrow_type}
    elif is_list_type(arrow_type):
        value = [encode_value(element, arrow_type.value_type, True) for element in value]
    elif pa.types.is_map(arrow_type):
        value = [
            [
                encode_value(key, arrow_type.key_type, True),
                encode_value(element, arrow_type.item_type, True),
            ]
            for key, element in value
        ]
    elif pa.types.is_boolean(arrow_type):
        return int(value)
    elif pa.types.is_temporal(arrow_type):
        physical = pa.int32() if pa.types.is_date32(arrow_type) or pa.types.is_time32(arrow_type) else pa.int64()
        value = pa.scalar(value, type=arrow_type).cast(physical).as_py()
        if nested or pa.types.is_duration(arrow_type):
            return str(value)
        storage_type = pa.timestamp(arrow_type.unit) if pa.types.is_timestamp(arrow_type) else arrow_type
        text = pa.scalar(value, type=physical).cast(storage_type).cast(pa.string()).as_py()
        return text.replace(" ", "T") + "Z" if pa.types.is_timestamp(arrow_type) else text
    elif pa.types.is_decimal(arrow_type):
        return format(value.copy_abs() if value == 0 else value, "f")
    elif pa.types.is_uint64(arrow_type):
        return str(value)
    elif nested and pa.types.is_int64(arrow_type):
        return str(value)
    elif nested and is_binary_type(arrow_type):
        return base64.b64encode(value).decode("ascii")
    else:
        return value
    return value if nested else json.dumps(value, sort_keys=True, separators=(",", ":"))


def decode_value(value, arrow_type, nested=False):
    """
    Convert a stored SQLite cell back to a value that Arrow can read
    """
    if value is None:
        return None
    if pa.types.is_struct(arrow_type):
        value = value if nested else json.loads(value)
        return {field.name: decode_value(value[field.name], field.type, True) for field in arrow_type}
    if is_list_type(arrow_type):
        value = value if nested else json.loads(value)
        return [decode_value(element, arrow_type.value_type, True) for element in value]
    if pa.types.is_map(arrow_type):
        value = value if nested else json.loads(value)
        return [
            (
                decode_value(key, arrow_type.key_type, True),
                decode_value(element, arrow_type.item_type, True),
            )
            for key, element in value
        ]
    if pa.types.is_boolean(arrow_type):
        return bool(value)
    if pa.types.is_temporal(arrow_type):
        if nested or pa.types.is_duration(arrow_type):
            return int(value)
        if pa.types.is_time(arrow_type):
            return pa.scalar("1970-01-01T" + value).cast(pa.timestamp(arrow_type.unit)).cast(pa.int64()).as_py()
        storage_type = pa.timestamp(arrow_type.unit) if pa.types.is_timestamp(arrow_type) else arrow_type
        text = value.removesuffix("Z") if pa.types.is_timestamp(arrow_type) else value
        return pa.scalar(text).cast(storage_type).cast(physical_arrow_type(arrow_type)).as_py()
    if pa.types.is_decimal(arrow_type):
        return decimal.Decimal(value)
    if pa.types.is_uint64(arrow_type):
        return int(value)
    if nested and pa.types.is_int64(arrow_type):
        return int(value)
    if nested and is_binary_type(arrow_type):
        return base64.b64decode(value)
    return value


def parse_edit_value(value, arrow_type):
    """
    Convert widget text to the column type before validating and saving the edit
    """
    if value is None:
        return None
    if pa.types.is_struct(arrow_type):
        value = json.loads(value) if isinstance(value, str) else value
        unknown = value.keys() - {field.name for field in arrow_type}
        if unknown:
            raise ValueError(f"Unknown nested DataFrame fields {unknown}")
        return {field.name: parse_edit_value(value.get(field.name), field.type) for field in arrow_type}
    if is_list_type(arrow_type):
        value = json.loads(value) if isinstance(value, str) else value
        return [parse_edit_value(element, arrow_type.value_type) for element in value]
    if pa.types.is_map(arrow_type):
        value = json.loads(value) if isinstance(value, str) else value
        return [
            (
                parse_edit_value(key, arrow_type.key_type),
                parse_edit_value(element, arrow_type.item_type),
            )
            for key, element in value
        ]
    if isinstance(value, str):
        if is_binary_type(arrow_type):
            return base64.b64decode(value, validate=True)
        if pa.types.is_time(arrow_type):
            return datetime.time.fromisoformat(value)
        if (
            pa.types.is_integer(arrow_type)
            or pa.types.is_floating(arrow_type)
            or pa.types.is_temporal(arrow_type)
            or pa.types.is_decimal(arrow_type)
        ):
            return pa.scalar(value).cast(arrow_type, safe=True).cast(physical_arrow_type(arrow_type)).as_py()
    return value


def encode_literal(value, arrow_type):
    """
    Convert a widget literal to the SQLite value compared by filters, row positions and identity lookups
    """
    value = pa.scalar(parse_edit_value(value, arrow_type), type=arrow_type).cast(physical_arrow_type(arrow_type))
    return encode_value(value.as_py(), arrow_type)


def validate_nullability(value, field):
    """
    Reject None in required fields, including nested fields that Arrow conversion alone allows
    """
    if value is None:
        if not field.nullable:
            raise ValueError(f"Column {field.name} does not allow null values")
        return
    if pa.types.is_struct(field.type):
        for child in field.type:
            validate_nullability(value[child.name], child)
    elif is_list_type(field.type):
        for element in value:
            validate_nullability(element, field.type.value_field)
    elif pa.types.is_map(field.type):
        for key, element in value:
            validate_nullability(key, field.type.key_field)
            validate_nullability(element, field.type.item_field)


class ArrowSnapshotProvider(DataFrameProvider):
    """
    Hold the Arrow table supplied to DataFrame before it is added to a Store.

    Queries use a temporary SQLite table. Adding this DataFrame to a Store copies
    its rows into that Store database, where they become editable.
    """

    def __init__(self, table: pa.Table):
        self.table = table
        self.query_provider = None

    def describe(self):
        return {
            "columns": [
                {
                    "name": field.name,
                    "type": str(field.type),
                    "nullable": field.nullable,
                }
                for field in self.table.schema
            ],
            "row_count": self.table.num_rows,
            "capabilities": {
                "source_filters": False,
                "edit": False,
                "changes": False,
            },
        }

    def query(self, options=None):
        """
        Use SQLite to filter and sort all supplied rows before returning the requested page
        """
        if self.query_provider is None:
            from pret.store import StoreDatabase

            self.query_provider = StoreDatabase().add_dataframe(self.table, uuid.uuid4().hex)
        return self.query_provider.query(options)

    def materialize(self, bounds=None):
        """
        Return the supplied Arrow table for Store creation or export
        """
        return self.table


class SQLiteDataFrameProvider(DataFrameProvider):
    """
    Fetch and edit one DataFrame table in a StoreDatabase.

    DataFrame delegates describe, query and row edits to this object after its data
    has been copied into a Store. Queries filter and sort the whole SQL table before
    returning a page. Edits use the Store database transaction and edit log.
    """

    def __init__(self, database: StoreDatabase, resource_id: str):
        self.database = database
        self.connection = database.connection
        self.resource_id = resource_id
        self.row_limit = 500
        row = self.connection.execute(
            "SELECT table_name,arrow_schema FROM _pret_dataframes WHERE resource_id=?",
            (resource_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown DataFrame resource {resource_id}")
        self.table_name = row[0]
        self.schema = pa.ipc.read_schema(pa.py_buffer(row[1]))

    def describe(self):
        """
        Return the stored column types, row count and revision number for DataFrame widgets
        """
        return {
            "columns": [
                {"name": field.name, "type": str(field.type), "nullable": field.nullable} for field in self.schema
            ],
            "row_count": self.connection.execute(f"SELECT count(*) FROM {quote_identifier(self.table_name)}").get,
            "revision": self.database.revision,
            "internal_id_col": INTERNAL_ROW_ID,
            "capabilities": {
                "source_filters": True,
                "edit": True,
                "changes": True,
                "query": True,
            },
        }

    def compile_filter(self, node):
        """
        Convert widget filter expressions to SQL and parameter values for query
        """
        if node is None:
            return "1", ()
        kind = node["kind"]
        if kind in {"and", "or"}:
            if not node["children"]:
                raise ValueError(f"Filter {kind} requires children")
            parts = [self.compile_filter(child) for child in node["children"]]
            return "(" + f" {kind.upper()} ".join(sql for sql, _ in parts) + ")", tuple(
                value for _, values in parts for value in values
            )
        if kind == "not":
            sql, values = self.compile_filter(node["child"])
            return f"NOT ({sql})", values
        fields = {field.name: field for field in self.schema}
        fields[INTERNAL_ROW_ID] = pa.field(INTERNAL_ROW_ID, pa.int64(), False)
        column = node["column"]
        if column not in fields:
            raise ValueError(f"Unknown DataFrame filter column {column}")
        name = quote_identifier(column)
        if kind == "relation":
            parent = SQLiteDataFrameProvider(self.database, node["resource_id"])
            parent_column = node["parent_column"]
            if parent_column not in parent.schema.names:
                raise ValueError(f"Unknown related column {parent_column}")
            if fields[column].type != parent.schema.field(parent_column).type:
                raise ValueError("Related columns must have matching Arrow types")
            sql, values = parent.compile_filter(node["filter"])
            return (
                f"{name} IN (SELECT {quote_identifier(parent_column)} "
                f"FROM {quote_identifier(parent.table_name)} WHERE {sql})",
                values,
            )
        if kind != "predicate":
            raise ValueError(f"Unsupported filter kind {kind}")
        operator = node["operator"]
        if operator in {"is_null", "is_not_null"}:
            return f"{name} IS {'NOT ' if operator == 'is_not_null' else ''}NULL", ()
        literal = node["value"]
        arrow_type = fields[column].type
        if operator in {"contains", "starts_with", "ends_with"}:
            if not (pa.types.is_string(arrow_type) or pa.types.is_large_string(arrow_type)):
                raise ValueError("Text filters require a string column")
            escaped = str(literal).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            patterns = {"contains": f"%{escaped}%", "starts_with": f"{escaped}%", "ends_with": f"%{escaped}"}
            return f"{name} LIKE ? ESCAPE '\\'", (patterns[operator],)
        if operator == "in":
            if not isinstance(literal, list):
                raise ValueError("IN filters require a list of values")
            if not literal:
                return "0", ()
            values = pa.array([parse_edit_value(value, arrow_type) for value in literal], type=arrow_type)
            values = values.cast(physical_arrow_type(arrow_type)).to_pylist()
            return f"{name} IN ({','.join('?' for _ in values)})", tuple(
                encode_value(value, arrow_type) for value in values
            )
        comparisons = {"eq": "IS", "ne": "IS NOT", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}
        if operator not in comparisons:
            raise ValueError(f"Unsupported filter operator {operator}")
        encoded = encode_literal(literal, arrow_type)
        comparison = query_column(column, arrow_type)
        return f"{comparison} {comparisons[operator]} ?", (numeric_key(encoded) if comparison != name else encoded,)

    def query(self, options=None):
        """
        Fetch rows and counts for a widget using one database read transaction.

        Filter and sort the full table before applying offset and limit. Optional ID
        lookups find selected rows or their positions even outside the displayed page.
        """
        options = options or {}
        fields = {field.name: field for field in self.schema}
        fields[INTERNAL_ROW_ID] = pa.field(INTERNAL_ROW_ID, pa.int64(), False)
        columns = options.get("columns", self.schema.names)
        distinct = options.get("distinct")
        if distinct is not None:
            columns = [distinct]
        elif INTERNAL_ROW_ID not in columns:
            columns = [INTERNAL_ROW_ID, *columns]
        if set(columns) - fields.keys():
            raise ValueError("Query columns must exist in the DataFrame schema")
        offset, limit = options.get("offset", 0), options.get("limit", self.row_limit)
        if not isinstance(offset, int) or not isinstance(limit, int) or offset < 0 or limit < 0:
            raise ValueError("Query offset and limit must be nonnegative integers")
        where, parameters = self.compile_filter(options.get("filter"))
        identities = options.get("identities")
        if identities is not None:
            where += f" AND {quote_identifier(INTERNAL_ROW_ID)} IN ({','.join('?' for _ in identities)})"
            parameters += tuple(identities)
        sort = options.get("sort", [])
        ordering = []
        for term in sort:
            if term["column"] not in fields or term["direction"] not in {"asc", "desc"}:
                raise ValueError("Sort terms require a known column and asc or desc direction")
            if distinct is not None and term["column"] != distinct:
                raise ValueError("Distinct queries can only sort their projected column")
            ordering.append(f"{query_column(term['column'], fields[term['column']].type)} {term['direction'].upper()}")
        ordering.append(quote_identifier(distinct or INTERNAL_ROW_ID))
        order = ", ".join(ordering)
        table = quote_identifier(self.table_name)
        source = f"FROM {table} WHERE {where}"
        selected = ", ".join(map(quote_identifier, columns))
        prefix = "DISTINCT " if distinct is not None else ""
        own_read = self.connection.get_autocommit()
        if own_read:
            self.connection.execute("BEGIN")
        try:
            total = self.connection.execute(
                f"SELECT count(*) FROM (SELECT {prefix}{selected} {source})",
                parameters,
            ).get
            records = self.connection.execute(
                f"SELECT {prefix}{selected} {source} ORDER BY {order} LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            )
            arrays = [[] for _ in columns]
            for row in records:
                for idx, column in enumerate(columns):
                    arrays[idx].append(decode_value(row[idx], fields[column].type))
            result = {
                "table": pa.Table.from_arrays(
                    [pa.array(values, type=fields[column].type) for column, values in zip(columns, arrays)],
                    schema=pa.schema([fields[column] for column in columns]),
                ),
                "total_count": total,
                "revision": self.database.revision,
                "offset": offset,
            }
            locate = options.get("locate")
            if locate is not None:
                if distinct is not None:
                    raise ValueError("Locate queries require row identities rather than distinct values")
                field = fields[locate["column"]]
                values = [encode_literal(value, field.type) for value in locate["values"]]
                positions = dict(
                    self.connection.execute(
                        "SELECT value,min(position) FROM ("
                        f"SELECT {quote_identifier(field.name)} AS value,"
                        f"row_number() OVER (ORDER BY {order})-1 AS position "
                        f"{source}) WHERE value IN ({','.join('?' for _ in values)}) OR value IS NULL GROUP BY value",
                        (*parameters, *values),
                    )
                )
                result["positions"] = [positions.get(value) for value in values]
            return result
        finally:
            if own_read:
                self.connection.execute("ROLLBACK")

    def materialize(self, bounds=None):
        """
        Return all rows as Arrow data for saving or export, checking any supplied row limit
        """
        bounds = bounds or {}
        count = self.describe()["row_count"]
        if bounds.get("max_rows") is not None and count > bounds["max_rows"]:
            raise ValueError(f"DataFrame exceeds the save bound of {bounds['max_rows']} rows")
        table = self.query({"limit": count})["table"].drop([INTERNAL_ROW_ID])
        return table

    def apply_edit(self, edit):
        """
        Validate a widget edit batch and save all its changes in the enclosing Store transaction.

        StoreDoc.dataframe_edit and StoreDoc.apply_browser_edit open that transaction, which
        records one acknowledgement for the whole request.

        1. Find the requested rows and check each changed value against its column type.
        2. Reject edits to cells changed since the widget last fetched them.
        3. Save the edits and add their row identities to the transaction acknowledgement.
        """
        if not self.database.in_transaction:
            raise RuntimeError("DataFrame edits must run inside a Store transaction")
        base_revision = int(edit["base_revision"])
        batch = edit.get("kind") == "batch"
        operations = edit["edits"] if batch else [edit]
        fields = {field.name: field for field in self.schema}
        with self.database.transaction() as action:
            # 1. Find the requested rows and check each changed value against its column type
            if base_revision > self.database.revision:
                raise ValueError("Edit base revision is newer than the Store")
            if not operations:
                raise ValueError("DataFrame batch requires at least one edit")
            updates, column_values = [], {}
            table = quote_identifier(self.table_name)
            for operation in operations:
                kind = operation.get("kind", "update")
                if kind not in {"update", "insert", "delete"}:
                    raise ValueError(f"Unsupported DataFrame edit kind {kind}")
                values = operation.get("values")
                if values is None:
                    values = {operation["column"]: operation["value"]} if kind == "update" else {}
                if set(values) - fields.keys() or kind == "update" and not values:
                    raise ValueError("DataFrame edits require known columns and nonempty updates")
                if kind == "insert":
                    values = {name: values.get(name) for name in fields}
                row_id = operation.get("row_identity")
                identity = operation.get("identity")
                if identity is not None:
                    if not identity or set(identity) - fields.keys():
                        raise ValueError("DataFrame identity must contain known columns")
                    where = " AND ".join(f"{quote_identifier(name)} IS ?" for name in identity)
                    rows = list(
                        self.connection.execute(
                            f"SELECT {quote_identifier(INTERNAL_ROW_ID)} FROM {table} WHERE {where} LIMIT 2",
                            tuple(encode_literal(value, fields[name].type) for name, value in identity.items()),
                        )
                    )
                    if len(rows) != 1:
                        raise ValueError("DataFrame identity must select exactly one row")
                    row_id = rows[0][0]
                values = dict(values)
                updates.append((kind, row_id, values))
                for name in values:
                    column_values.setdefault(name, []).append(values)
            for name, targets in column_values.items():
                field = fields[name]
                normalized = pa.array(
                    [parse_edit_value(values[name], field.type) for values in targets],
                    type=field.type,
                )
                normalized = normalized.cast(physical_arrow_type(field.type))
                for values, value in zip(targets, normalized.to_pylist()):
                    validate_nullability(value, field)
                    values[name] = encode_value(value, field.type)
            # 2. Reject edits to cells changed since the widget last fetched them
            for kind, row_id, values in updates:
                later = self.connection.execute(
                    "SELECT kind,column_name FROM _pret_revision_rows WHERE resource_id=? AND revision>? "
                    "AND revision<? AND row_identity=?",
                    (self.resource_id, base_revision, action["revision"], row_id),
                )
                if any(
                    later_kind != "update" or kind != "update" or set(values) & set(json.loads(columns))
                    for later_kind, columns in later
                ):
                    return {
                        "status": "conflict",
                        "revision": action["revision"],
                        "row_identity": row_id,
                        "request_id": action["request_id"],
                    }
            # 3. Return each edit group to the enclosing transaction for its single acknowledgement
            row_ids = []
            for kind, row_id, values in updates:
                if kind == "update":
                    self.connection.execute(
                        f"UPDATE {table} SET {', '.join(f'{quote_identifier(name)}=?' for name in values)} "
                        f"WHERE {quote_identifier(INTERNAL_ROW_ID)}=?",
                        (*values.values(), row_id),
                    )
                elif kind == "delete":
                    self.connection.execute(
                        f"DELETE FROM {table} WHERE {quote_identifier(INTERNAL_ROW_ID)}=?", (row_id,)
                    )
                elif values:
                    self.connection.execute(
                        f"INSERT INTO {table} ({','.join(map(quote_identifier, values))}) "
                        f"VALUES({','.join('?' for _ in values)})",
                        tuple(values.values()),
                    )
                    row_id = self.connection.last_insert_rowid()
                else:
                    self.connection.execute(f"INSERT INTO {table} DEFAULT VALUES")
                    row_id = self.connection.last_insert_rowid()
                if self.connection.changes() != 1:
                    raise KeyError(f"Unknown row identity {row_id}")
                row_ids.append(row_id)
                self.connection.execute(
                    "INSERT INTO _pret_revision_rows VALUES(?,?,?,?,?)",
                    (
                        self.resource_id,
                        action["revision"],
                        row_id,
                        json.dumps(sorted(values)),
                        kind,
                    ),
                )
            result = {"status": "applied", "revision": action["revision"], "request_id": action["request_id"]}
            result["row_identities" if batch else "row_identity"] = row_ids if batch else row_ids[0]
            action.setdefault("row_identities", []).append(row_ids)
        return result
