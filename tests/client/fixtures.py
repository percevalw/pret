"""
Write static Python exports used by browser Arrow and standalone Store tests
"""

import json
import sys
from decimal import Decimal
from pathlib import Path

import pyarrow as pa

from pret import DataFrame, create_store
from pret.arrow import serialize_arrow_as_bytes
from pret.manager import b64_encode
from pret.marshal import PretMarshaler

instant = 1789381234567890123
nested = pa.list_(pa.struct([("instant", pa.timestamp("ns")), ("binary", pa.binary()), ("missing", pa.string())]))
table = pa.table(
    {
        "integer": pa.array([9007199254740993, -1], type=pa.int64()),
        "unsigned": pa.array([18446744073709551615, 1], type=pa.uint64()),
        "instant": pa.array([instant, instant + 1], type=pa.timestamp("ns")),
        "amount": pa.array(
            [Decimal("999999999999999999999999999999999999.99"), Decimal("-1.23")],
            type=pa.decimal128(38, 2),
        ),
        "nested": pa.array(
            [[{"instant": instant, "binary": bytes([0, 255, 128]), "missing": None}], None],
            type=nested,
        ),
    }
)
Path(sys.argv[1], "typed.json").write_text(json.dumps(list(serialize_arrow_as_bytes(table))))


standalone = create_store(
    {
        "todo": {"milk": False},
        "rows": DataFrame([{"id": 1, "label": "old", "spans": [{"label": "tag"}]}]),
    }
)
standalone["rows"].update(identity={"id": 1}, values={"label": "prepared"})
marshaler = PretMarshaler()
(data, code), chunk = marshaler.dump(standalone)
result = {"bytes": b64_encode(data), "code": code, "chunk": chunk, "id": marshaler.id}
Path(sys.argv[1], "standalone.json").write_text(json.dumps(result))
