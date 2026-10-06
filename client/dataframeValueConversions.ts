import * as arrow from "apache-arrow";
import { DataType } from "apache-arrow";

// Arrow cells keep exact integers and binary values until conversion for SQL or widgets
// Schema branches select the corresponding value type when building or reading a cell
export type ArrowValue =
  | null
  | boolean
  | number
  | bigint
  | string
  | Uint8Array
  | ArrowValue[]
  | { [key: string]: ArrowValue };
export type SqlValue = null | number | bigint | string | Uint8Array;
type ScalarValue = null | boolean | number | string;
type JsonValue = ScalarValue | JsonValue[] | { [key: string]: JsonValue };
// Widget values, lists and structs stay arrays and objects as Python reads them
export type DisplayValue = JsonValue;

const two = BigInt(2);
const shift128 = two ** BigInt(127);

// Reject Arrow column types that the local SQLite converter cannot preserve
export function checkType(type: arrow.DataType): void {
  const supported = [
    arrow.Type.Null,
    arrow.Type.Int,
    arrow.Type.Float,
    arrow.Type.Bool,
    arrow.Type.Utf8,
    arrow.Type.LargeUtf8,
    arrow.Type.Binary,
    arrow.Type.LargeBinary,
    arrow.Type.FixedSizeBinary,
    arrow.Type.Timestamp,
    arrow.Type.Time,
    arrow.Type.Date,
    arrow.Type.Duration,
    arrow.Type.Decimal,
    arrow.Type.Struct,
    arrow.Type.List,
    arrow.Type.FixedSizeList,
  ];
  if (!supported.includes(type.typeId) || (DataType.isDecimal(type) && type.bitWidth !== 128)) {
    throw new Error(`Unsupported editable Arrow type ${type}`);
  }
  for (const field of type.children || []) checkType(field.type);
}

// PRET compares decimal and integer text without converting through floating point
export function numericKey(value: string | null): string | null {
  if (value === null) return null;
  const digits = value.replace(".", "").replace(/^[-0]+/, "") || "0";
  const key = String(digits.length).padStart(10, "0") + digits;
  return value.startsWith("-") ? "0" + key.replace(/\d/g, (digit) => String(9 - Number(digit))) : "1" + key;
}

export function numericText(type: arrow.DataType): boolean {
  return DataType.isDecimal(type) || DataType.isDuration(type) ||
    (DataType.isInt(type) && type.bitWidth === 64 && !type.isSigned);
}

function base64(bytes: Uint8Array): string {
  let text = "";
  for (const byte of bytes) text += String.fromCharCode(byte);
  return btoa(text);
}

function binary(text: string): Uint8Array {
  return Uint8Array.from(atob(text), (char) => char.charCodeAt(0));
}

function isTemporalType(type: arrow.DataType): type is arrow.Timestamp | arrow.Date_ | arrow.Time | arrow.Duration {
  return [arrow.Type.Timestamp, arrow.Type.Date, arrow.Type.Time, arrow.Type.Duration].includes(type.typeId);
}

// Read timestamp counts from Arrow buffers so nanoseconds are not rounded to milliseconds
export function arrowValue(vector: arrow.Vector, idx: number): ArrowValue {
  if (!vector.isValid(idx)) return null;
  const type = vector.type;
  if (isTemporalType(type)) {
    for (const data of vector.data) {
      if (idx < data.length) return BigInt(data.values[idx]);
      idx -= data.length;
    }
  }
  if (DataType.isStruct(type)) {
    return Object.fromEntries(type.children.map((field, pos) => [field.name, arrowValue(vector.getChildAt(pos), idx)]));
  }
  if (DataType.isList(type) || DataType.isFixedSizeList(type)) {
    const child: arrow.Vector = vector.get(idx);
    return Array.from({ length: child.length }, (_, pos) => arrowValue(child, pos));
  }
  if (DataType.isDecimal(type)) {
    const words = vector.get(idx) as Uint32Array;
    let coefficient = BigInt(0);
    for (let pos = words.length - 1; pos >= 0; pos--)
      coefficient = (coefficient << BigInt(32)) | BigInt(words[pos] >>> 0);
    return coefficient >= shift128 ? coefficient - two ** BigInt(128) : coefficient;
  }
  return vector.get(idx);
}

// Parse widget input as the column type before saving it in SQLite
export function parseCellValue(value: unknown, field: arrow.Field): ArrowValue {
  const type = field.type;
  if (value === null || value === undefined) {
    if (!field.nullable) throw new Error(`${field.name} cannot be null`);
    return null;
  }
  if (DataType.isBool(type)) {
    if (value === true || value === "true") return true;
    if (value === false || value === "false") return false;
    throw new Error(`${field.name} requires a boolean`);
  }
  if (DataType.isInt(type)) {
    if (typeof value === "number" && !Number.isSafeInteger(value))
      throw new Error(`${field.name} requires an exact integer`);
    if (!/^[+-]?\d+$/.test(String(value))) throw new Error(`${field.name} requires an integer`);
    const number = BigInt(String(value));
    const bits = BigInt(type.bitWidth);
    const min = type.isSigned ? -(two ** (bits - BigInt(1))) : BigInt(0);
    const max = two ** (bits - BigInt(type.isSigned ? 1 : 0)) - BigInt(1);
    if (number < min || number > max) throw new Error(`${field.name} is outside its integer range`);
    return type.bitWidth === 64 ? number : Number(number);
  }
  if (DataType.isFloat(type)) {
    const number = Number(value);
    if (!Number.isFinite(number) || value === "") throw new Error(`${field.name} requires a finite number`);
    return number;
  }
  if (DataType.isDecimal(type)) {
    const match = String(value).match(/^([+-]?)(\d+)(?:\.(\d+))?$/);
    if (!match) throw new Error(`${field.name} requires a decimal`);
    const fractional = match[3] || "";
    if (fractional.length > Math.max(0, type.scale)) throw new Error(`${field.name} exceeds its decimal scale`);
    let coefficient = BigInt(
      (match[1] === "-" ? "-" : "") + match[2] + fractional.padEnd(Math.max(0, type.scale), "0")
    );
    if (type.scale < 0) {
      const factor = BigInt(10) ** BigInt(-type.scale);
      if (coefficient % factor) throw new Error(`${field.name} exceeds its decimal scale`);
      coefficient /= factor;
    }
    if (
      (coefficient < BigInt(0) ? -coefficient : coefficient) >= two ** BigInt(127) ||
      coefficient.toString().replace("-", "").length > type.precision
    )
      throw new Error(`${field.name} exceeds its decimal precision`);
    return coefficient;
  }
  if (isTemporalType(type)) {
    if (typeof value === "number" && !Number.isSafeInteger(value))
      throw new Error(`${field.name} requires exact integer temporal units`);
    if (typeof value === "bigint" || /^-?\d+$/.test(String(value))) {
      const number = BigInt(String(value));
      const bits = BigInt(
        (DataType.isTime(type) && type.bitWidth === 32) || (DataType.isDate(type) && type.unit === 0) ? 32 : 64
      );
      if (number < -(two ** (bits - BigInt(1))) || number >= two ** (bits - BigInt(1)))
        throw new Error(`${field.name} is outside its temporal integer range`);
      return number;
    }
    const text = String(value);
    if (DataType.isDuration(type)) throw new Error(`${field.name} requires integer duration units`);
    if (DataType.isTime(type)) {
      const match = text.match(/^(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?$/);
      if (!match || Number(match[1]) > 23 || Number(match[2]) > 59 || Number(match[3]) > 59)
        throw new Error(`${field.name} requires a time`);
      const ns =
        BigInt(Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3])) * BigInt(1000000000) +
        BigInt((match[4] || "").padEnd(9, "0"));
      return parseCellValue(ns / [BigInt(1000000000), BigInt(1000000), BigInt(1000), BigInt(1)][type.unit], field);
    }
    const milliseconds = Date.parse(text);
    if (!Number.isFinite(milliseconds)) throw new Error(`${field.name} requires an ISO date or timestamp`);
    if (DataType.isDate(type))
      return parseCellValue(BigInt(type.unit === 0 ? Math.floor(milliseconds / 86400000) : milliseconds), field);
    const fraction = text.match(/\.(\d+)/)?.[1] || "";
    const ns = BigInt(milliseconds) * BigInt(1000000) + BigInt(fraction.padEnd(9, "0").slice(3, 9));
    return parseCellValue(ns / [BigInt(1000000000), BigInt(1000000), BigInt(1000), BigInt(1)][type.unit], field);
  }
  if ([arrow.Type.Binary, arrow.Type.LargeBinary, arrow.Type.FixedSizeBinary].includes(type.typeId)) {
    const bytes = typeof value === "string" ? binary(value) : value;
    if (!(bytes instanceof Uint8Array) || (DataType.isFixedSizeBinary(type) && bytes.length !== type.byteWidth))
      throw new Error(`${field.name} requires binary data of the declared size`);
    return bytes;
  }
  if (DataType.isStruct(type) || DataType.isList(type) || DataType.isFixedSizeList(type)) {
    const nested: unknown = typeof value === "string" ? JSON.parse(value) : value;
    if (DataType.isStruct(type)) {
      if (!nested || Array.isArray(nested) || typeof nested !== "object")
        throw new Error(`${field.name} requires an object`);
      const known = new Set(type.children.map((child) => child.name));
      if (Object.keys(nested).some((key) => !known.has(key)))
        throw new Error(`${field.name} contains an unknown field`);
      return Object.fromEntries(
        type.children.map((child) => [
          child.name,
          parseCellValue((nested as Record<string, unknown>)[child.name], child),
        ])
      );
    }
    if (!Array.isArray(nested) || (DataType.isFixedSizeList(type) && nested.length !== type.listSize))
      throw new Error(`${field.name} requires an array of the declared size`);
    return nested.map((entry) => parseCellValue(entry, type.children[0]));
  }
  if ([arrow.Type.Utf8, arrow.Type.LargeUtf8].includes(type.typeId)) {
    if (typeof value !== "string") throw new Error(`${field.name} requires text`);
    return value;
  }
  throw new Error(`Unsupported editable Arrow type ${type}`);
}

export function sqlType(type: arrow.DataType): "BLOB" | "INTEGER" | "REAL" | "TEXT" {
  if (numericText(type) || isTemporalType(type)) return "TEXT";
  if ([arrow.Type.Binary, arrow.Type.LargeBinary, arrow.Type.FixedSizeBinary].includes(type.typeId)) return "BLOB";
  if (DataType.isInt(type) || DataType.isBool(type)) return "INTEGER";
  return DataType.isFloat(type) ? "REAL" : "TEXT";
}

// Encode Arrow cells for SQLite, nested JSON preserves exact numbers and timestamps
export function encodeSqlValue(value: ArrowValue, type: arrow.DataType): SqlValue {
  if (value === null) return null;
  if (typeof value === "number" && Number.isNaN(value))
    throw new Error("NaN values require conversion to Arrow nulls before editing");
  if (DataType.isBool(type)) return Number(value);
  if (DataType.isDecimal(type)) return String(displayValue(value, type));
  if (isTemporalType(type)) return String(displayValue(value, type));
  if (DataType.isInt(type) && type.bitWidth === 64 && !type.isSigned)
    return String(value);
  if ([arrow.Type.Struct, arrow.Type.List, arrow.Type.FixedSizeList].includes(type.typeId)) {
    function nested(entry: ArrowValue, dataType: arrow.DataType): JsonValue {
      if (entry === null) return null;
      if (typeof entry === "number" && !Number.isFinite(entry))
        throw new Error("Nested nonfinite values require conversion to Arrow nulls before editing");
      if (DataType.isStruct(dataType))
        return Object.fromEntries(
          [...dataType.children]
            .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
            .map((field) => [field.name, nested((entry as Record<string, ArrowValue>)[field.name], field.type)])
        );
      if (DataType.isList(dataType) || DataType.isFixedSizeList(dataType))
        return (entry as ArrowValue[]).map((child) => nested(child, dataType.children[0].type));
      if (DataType.isDecimal(dataType)) return displayValue(entry, dataType);
      return typeof entry === "bigint"
        ? String(entry)
        : entry instanceof Uint8Array
        ? base64(entry)
        : (entry as DisplayValue);
    }
    return JSON.stringify(nested(value, type));
  }
  return value as SqlValue;
}

// BrowserDatabase query decodes SQLite cells before makeArrowColumn builds the returned Arrow page
export function decodeSqlValue(value: SqlValue, type: arrow.DataType): ArrowValue {
  if (value === null) return null;
  if (DataType.isBool(type)) return Boolean(value);
  if (DataType.isDecimal(type)) return parseCellValue(value, new arrow.Field("value", type, true));
  if (DataType.isInt(type) && type.bitWidth === 64)
    return BigInt(value as string | number | bigint);
  if (isTemporalType(type)) return parseCellValue(value, new arrow.Field("value", type, true));
  if ([arrow.Type.Struct, arrow.Type.List, arrow.Type.FixedSizeList].includes(type.typeId))
    return restoreNested(JSON.parse(value as string), type);
  return value;
}

function restoreNested(value: JsonValue, type: arrow.DataType): ArrowValue {
  if (value === null) return null;
  if (DataType.isStruct(type))
    return Object.fromEntries(
      type.children.map((field) => [
        field.name,
        restoreNested((value as Record<string, JsonValue>)[field.name], field.type),
      ])
    );
  if (DataType.isList(type) || DataType.isFixedSizeList(type))
    return (value as JsonValue[]).map((entry) => restoreNested(entry, type.children[0].type));
  if ([arrow.Type.Binary, arrow.Type.LargeBinary, arrow.Type.FixedSizeBinary].includes(type.typeId))
    return binary(value as string);
  if (DataType.isDecimal(type)) return parseCellValue(value, new arrow.Field("value", type, true));
  if (isTemporalType(type) || (DataType.isInt(type) && type.bitWidth === 64))
    return BigInt(value as string | number | bigint);
  return value;
}

// Fill timestamp buffers directly because Arrow builders would otherwise round the values to milliseconds
export function makeArrowColumn(values: ArrowValue[], type: arrow.DataType): arrow.Vector {
  function prepare(value: ArrowValue, dataType: arrow.DataType): unknown {
    if (value === null) return null;
    if (isTemporalType(dataType)) return 0;
    if (DataType.isDecimal(dataType)) {
      const integer = value as bigint;
      let coefficient = integer < BigInt(0) ? integer + two ** BigInt(128) : integer;
      const words = new Uint32Array(4);
      for (let idx = 0; idx < 4; idx++, coefficient >>= BigInt(32))
        words[idx] = Number(coefficient & BigInt(0xffffffff));
      return words;
    }
    if (DataType.isStruct(dataType))
      return Object.fromEntries(
        dataType.children.map((field) => [
          field.name,
          prepare((value as Record<string, ArrowValue>)[field.name], field.type),
        ])
      );
    if (DataType.isList(dataType) || DataType.isFixedSizeList(dataType))
      return (value as ArrowValue[]).map((entry) => prepare(entry, dataType.children[0].type));
    return value;
  }
  function fill(data: arrow.Data, original: ArrowValue[], dataType: arrow.DataType): void {
    if (isTemporalType(dataType)) {
      for (let idx = 0; idx < original.length; idx++) {
        if (original[idx] !== null)
          data.values[idx] =
            data.values instanceof BigInt64Array ? BigInt(original[idx] as bigint) : Number(original[idx]);
      }
    } else if (DataType.isStruct(dataType)) {
      dataType.children.forEach((field, idx) =>
        fill(
          data.children[idx],
          original.map((value) => (value === null ? null : (value as Record<string, ArrowValue>)[field.name])),
          field.type
        )
      );
    } else if (DataType.isList(dataType) || DataType.isFixedSizeList(dataType)) {
      fill(
        data.children[0],
        original.flatMap(
          (value) => value || (DataType.isFixedSizeList(dataType) ? Array(dataType.listSize).fill(null) : [])
        ),
        dataType.children[0].type
      );
    }
  }
  const vector = arrow.vectorFromArray(
    values.map((value) => prepare(value, type)),
    type
  );
  let offset = 0;
  for (const data of vector.data) {
    fill(data, values.slice(offset, offset + data.length), type);
    offset += data.length;
  }
  return vector;
}

// Convert Arrow cells to widget values, nested cells become JSON text
export function displayValue(value: ArrowValue, type: arrow.DataType): DisplayValue {
  if (value === null) return null;
  if (DataType.isDecimal(type)) {
    const coefficient = value as bigint;
    const negative = coefficient < BigInt(0);
    if (type.scale < 0) return String(coefficient * BigInt(10) ** BigInt(-type.scale));
    const digits = String(negative ? -coefficient : coefficient).padStart(type.scale + 1, "0");
    return (
      (negative ? "-" : "") + (type.scale ? digits.slice(0, -type.scale) + "." + digits.slice(-type.scale) : digits)
    );
  }
  if (DataType.isTimestamp(type) || DataType.isDate(type)) {
    if (DataType.isDate(type))
      return new Date(Number(value) * (type.unit === 0 ? 86400000 : 1)).toISOString().slice(0, 10);
    const precision = [0, 3, 6, 9][type.unit];
    const units = BigInt(10) ** BigInt(precision);
    const remainder = ((BigInt(value as string | number | bigint) % units) + units) % units;
    const iso = new Date(Number((BigInt(value as string | number | bigint) - remainder) / units) * 1000).toISOString();
    return iso.slice(0, 19) + (precision ? "." + String(remainder).padStart(precision, "0") : "") + "Z";
  }
  if (DataType.isTime(type)) {
    const precision = [0, 3, 6, 9][type.unit];
    const units = BigInt(10) ** BigInt(precision);
    const seconds = Number(BigInt(value as string | number | bigint) / units);
    const text = [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60]
      .map((part) => String(part).padStart(2, "0"))
      .join(":");
    return (
      text + (precision ? "." + String(BigInt(value as string | number | bigint) % units).padStart(precision, "0") : "")
    );
  }
  if ([arrow.Type.Struct, arrow.Type.List, arrow.Type.FixedSizeList].includes(type.typeId)) {
    function nested(entry: ArrowValue, dataType: arrow.DataType): JsonValue {
      if (entry === null) return null;
      if (DataType.isStruct(dataType))
        return Object.fromEntries(
          dataType.children.map((field) => [
            field.name,
            nested((entry as Record<string, ArrowValue>)[field.name], field.type),
          ])
        );
      if (DataType.isList(dataType) || DataType.isFixedSizeList(dataType))
        return (entry as ArrowValue[]).map((child) => nested(child, dataType.children[0].type));
      return displayValue(entry, dataType);
    }
    return nested(value, type);
  }
  if ([arrow.Type.Binary, arrow.Type.LargeBinary, arrow.Type.FixedSizeBinary].includes(type.typeId))
    return base64(value as Uint8Array);
  if (typeof value === "bigint")
    return value <= BigInt(Number.MAX_SAFE_INTEGER) && value >= BigInt(Number.MIN_SAFE_INTEGER)
      ? Number(value)
      : String(value);
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return value;
  throw new Error(`Unsupported display Arrow type ${type}`);
}

// ClientDataFrame calls this to turn Arrow query pages into widget rows
export function decodeDataFrameRows(bytes: Uint8Array): Record<string, DisplayValue>[] {
  const table = arrow.tableFromIPC(bytes);
  return Array.from({ length: table.numRows }, (_, idx) =>
    Object.fromEntries(
      table.schema.fields.map((field) => [
        field.name,
        displayValue(arrowValue(table.getChild(field.name), idx), field.type),
      ])
    )
  );
}
