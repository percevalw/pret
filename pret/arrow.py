"""
Convert Arrow tables to bytes for DataFrame responses and check their size
"""

import pyarrow as pa
import pyarrow.ipc as ipc


def calculate_arrow_size(table) -> int:
    """
    Measure the Arrow message size before returning a page or saving a DataFrame
    """
    output = pa.MockOutputStream()
    with ipc.new_stream(output, table.schema) as writer:
        writer.write_table(table)
    return output.tell()


def serialize_arrow_as_bytes(table) -> bytes:
    """
    Encode an Arrow table as bytes for a browser response or standalone export
    """
    output = pa.BufferOutputStream()
    with ipc.new_stream(output, table.schema) as writer:
        writer.write_table(table)
    return output.getvalue().to_pybytes()
