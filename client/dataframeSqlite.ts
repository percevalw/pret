import * as arrow from "apache-arrow";
import type { QueryOptions, QueryResult, DataFrameDescription, DataFrameEdit, DataFrameEditResult } from "./dataframe";
import {
  arrowValue,
  numericKey,
  numericText,
  checkType,
  decodeSqlValue,
  makeArrowColumn,
  parseCellValue,
  sqlType,
  encodeSqlValue,
} from "./dataframeValueConversions";

// Row ID column added to every Store table, pret.dataframe_sqlite.INTERNAL_ROW_ID in Python
export const INTERNAL_ROW_ID = "__pret_row_id";
const identity = INTERNAL_ROW_ID;
const quote = (name: string) => '"' + name.replace(/"/g, '""') + '"';
let sqlitePromise: Promise<any>;

export type BrowserResource = {
  resource_id: string;
  bytes: Uint8Array;
  batches?: Uint8Array[];
};
type Resource = { table: string; fields: arrow.Field[] };

// Complete local tables for browser only Stores, connected full copies and loaded source previews.
// Python decides edits and undo for connected Stores. Local edits stay inside a savepoint until StoreController
// learns the result, then rollback removes them and applyCommits writes the rows Python accepted
export class BrowserDatabase {
  private resources = new Map<string, Resource>();
  private pending = false;
  private temporaryIdentity = -1;
  private db: any;
  // Store revision reported by describe and query, set by StoreController
  revision = 0;

  constructor(sqlite: any, inputs: BrowserResource[]) {
    this.db = new sqlite.oo1.DB(":memory:");
    this.db.createFunction({ name: "_pret_numeric_key", xFunc: (_context, value) => numericKey(value), deterministic: true });
    try {
      this.importTables(inputs);
    } catch (error) {
      this.db.close();
      throw error;
    }
  }

  // Import complete Arrow tables, including additional batches from a Store snapshot
  importTables(inputs: BrowserResource[]): void {
    const before = new Map(this.resources);
    try {
      this.db.transaction(() => {
        for (const input of inputs) {
          if (this.resources.has(input.resource_id)) throw new Error(`Duplicate DataFrame ${input.resource_id}`);
          const table = arrow.tableFromIPC(input.bytes);
          const fields = table.schema.fields.filter((field) => field.name !== identity);
          fields.forEach((field) => checkType(field.type));
          const name = `frame_${this.resources.size}`;
          this.db.exec(
            `CREATE TABLE ${quote(name)} (${quote(identity)} INTEGER PRIMARY KEY AUTOINCREMENT${fields
              .map((field) => `,${quote(field.name)} ${sqlType(field.type)}${field.nullable ? "" : " NOT NULL"}`)
              .join("")})`
          );
          const resource = { table: name, fields };
          this.resources.set(input.resource_id, resource);
          const statement = this.db.prepare(
            `INSERT INTO ${quote(name)} VALUES(${Array(fields.length + 1)
              .fill("?")
              .join(",")})`
          );
          try {
            for (const bytes of [input.bytes, ...(input.batches || [])]) {
              const batch = bytes === input.bytes ? table : arrow.tableFromIPC(bytes);
              const ids = batch.getChild(identity);
              const columns = fields.map((field) => batch.getChild(field.name));
              for (let idx = 0; idx < batch.numRows; idx++) {
                const rowId = ids?.get(idx) ?? idx + 1;
                statement
                  .bind([
                    rowId,
                    ...fields.map((field, pos) => encodeSqlValue(arrowValue(columns[pos], idx), field.type)),
                  ])
                  .stepReset();
              }
            }
          } finally {
            statement.finalize();
          }
        }
      });
    } catch (error) {
      this.resources = before;
      throw error;
    }
  }

  private resource(id: string): Resource {
    const resource = this.resources.get(id);
    if (!resource) throw new Error(`Unknown DataFrame ${id}`);
    return resource;
  }

  private field(resource: Resource, name: string): arrow.Field {
    if (name === identity) return new arrow.Field(identity, new arrow.Int64(), false);
    const field = resource.fields.find((field) => field.name === name);
    if (!field) throw new Error(`Unknown DataFrame column ${name}`);
    return field;
  }

  // Return column types, row count and the last saved revision number for table and form widgets
  describe(id: string): DataFrameDescription {
    const resource = this.resource(id);
    return {
      columns: resource.fields.map((field) => ({
        name: field.name,
        type: String(field.type),
        nullable: field.nullable,
      })),
      row_count: Number(this.db.selectValue(`SELECT count(*) FROM ${quote(resource.table)}`)),
      revision: this.revision,
      internal_id_col: identity,
      // The whole table is in the page, header filters already search all of it, no source query is needed
      capabilities: { edit: true, source_filters: false, changes: true },
    };
  }

  // Convert widget filters to SQL with parameters, including filters referring to another table in this Store
  private predicate(resource: Resource, node: any, values: any[]): string {
    if (!node) return "1";
    if (node.kind === "and" || node.kind === "or") {
      if (!node.children.length) throw new Error(`Filter ${node.kind} requires children`);
      return (
        "(" +
        node.children
          .map((child) => this.predicate(resource, child, values))
          .join(node.kind === "and" ? " AND " : " OR ") +
        ")"
      );
    }
    if (node.kind === "not") return `NOT (${this.predicate(resource, node.child, values)})`;
    const field = this.field(resource, node.column);
    const column = quote(field.name);
    if (node.kind === "relation") {
      const parent = this.resource(node.resource_id);
      if (String(field.type) !== String(this.field(parent, node.parent_column).type))
        throw new Error("Related columns must have matching Arrow types");
      return `${column} IN (SELECT ${quote(node.parent_column)} FROM ${quote(parent.table)} WHERE ${this.predicate(
        parent,
        node.filter,
        values
      )})`;
    }
    if (node.kind !== "predicate") throw new Error(`Unknown filter kind ${node.kind}`);
    if (node.operator === "is_null") return `${column} IS NULL`;
    if (node.operator === "is_not_null") return `${column} IS NOT NULL`;
    const bind = (value) =>
      values.push(encodeSqlValue(parseCellValue(value, new arrow.Field(field.name, field.type, true)), field.type));
    if (node.operator === "in") {
      if (!node.value.length) return "0";
      node.value.forEach(bind);
      return `${column} IN (${node.value.map(() => "?").join(",")})`;
    }
    const operators = {
      eq: "IS",
      ne: "IS NOT",
      lt: "<",
      lte: "<=",
      gt: ">",
      gte: ">=",
    };
    if (operators[node.operator]) {
      bind(node.value);
      if (numericText(field.type)) {
        values[values.length - 1] = numericKey(values[values.length - 1]);
        return `_pret_numeric_key(${column}) ${operators[node.operator]} ?`;
      }
      return `${column} ${operators[node.operator]} ?`;
    }
    if (["icontains", "contains", "starts_with", "ends_with"].includes(node.operator)) {
      if (![arrow.Type.Utf8, arrow.Type.LargeUtf8].includes(field.type.typeId))
        throw new Error("Text filters require a text column");
      const text = String(node.value);
      // LIKE ignores case for icontains, GLOB matches case for the other text operators, as in dataframe_sqlite.py
      if (node.operator === "icontains") {
        values.push(`%${text.replace(/\\/g, "\\\\").replace(/%/g, "\\%").replace(/_/g, "\\_")}%`);
        return `${column} LIKE ? ESCAPE '\\'`;
      }
      const escaped = text.replace(/[[*?]/g, "[$&]");
      values.push(
        node.operator === "contains" ? `*${escaped}*` : node.operator === "starts_with" ? `${escaped}*` : `*${escaped}`
      );
      return `${column} GLOB ?`;
    }
    throw new Error(`Unknown filter operator ${node.operator}`);
  }

  // Filter and sort all rows before returning the requested page as Arrow data
  query(id: string, options: QueryOptions = {}): QueryResult {
    const resource = this.resource(id);
    const values = [];
    let where = this.predicate(resource, options.filter, values);
    if (options.identities) {
      values.push(...options.identities.map(BigInt));
      where += options.identities.length
        ? ` AND ${quote(identity)} IN (${options.identities.map(() => "?").join(",")})`
        : " AND 0";
    }
    const offset = options.offset ?? 0;
    const limit = options.limit ?? 500;
    if (!Number.isSafeInteger(offset) || offset < 0 || !Number.isSafeInteger(limit) || limit < 0)
      throw new Error("Query offset and limit must be nonnegative integers");
    const sort = (options.sort || []).map(({ column, direction }) => {
      const field = this.field(resource, column);
      if (options.distinct && column !== options.distinct)
        throw new Error("Distinct queries can only sort their projected column");
      if (direction !== "asc" && direction !== "desc") throw new Error("Invalid sort direction");
      const expression = numericText(field.type) ? `_pret_numeric_key(${quote(column)})` : quote(column);
      return `${expression} ${direction.toUpperCase()}`;
    });
    if (!options.distinct) sort.push(quote(identity));
    const source = `FROM ${quote(resource.table)} WHERE ${where}`;
    if (options.locate) {
      const field = this.field(resource, options.locate.column);
      const ranked = `SELECT ${quote(field.name)} AS value,row_number() OVER(ORDER BY ${sort.join(
        ","
      )})-1 AS position ${source}`;
      const positions = options.locate.values.map(
        (value) =>
          this.db.selectValue(`SELECT min(position) FROM (${ranked}) WHERE value IS ?`, [
            ...values,
            encodeSqlValue(parseCellValue(value, field), field.type),
          ]) ?? null
      );
      return {
        positions,
        total_count: Number(this.db.selectValue(`SELECT count(*) ${source}`, values.length ? values : undefined)),
        revision: this.revision,
      };
    }
    const names = options.distinct
      ? [options.distinct]
      : [
          identity,
          ...(options.columns || resource.fields.map((field) => field.name)).filter((name) => name !== identity),
        ];
    const fields = names.map((name) => this.field(resource, name));
    const selected = names.map(quote).join(",");
    const total = Number(
      this.db.selectValue(
        options.distinct ? `SELECT count(*) FROM (SELECT DISTINCT ${selected} ${source})` : `SELECT count(*) ${source}`,
        values.length ? values : undefined
      )
    );
    const sql = options.distinct
      ? `SELECT DISTINCT ${selected} ${source} ORDER BY ${sort.join(",") || selected} LIMIT ? OFFSET ?`
      : `SELECT ${selected} ${source} ORDER BY ${sort.join(",")} LIMIT ? OFFSET ?`;
    const rows = this.db.selectArrays(sql, [...values, limit, offset]);
    const columns = Object.fromEntries(
      fields.map((field, idx) => [
        field.name,
        makeArrowColumn(
          rows.map((row) => decodeSqlValue(row[idx], field.type)),
          field.type
        ),
      ])
    );
    const table = rows.length
      ? new arrow.Table(new arrow.Schema(fields), columns)
      : new arrow.Table(new arrow.Schema(fields));
    return {
      bytes: arrow.tableToIPC(table),
      row_count: rows.length,
      total_count: total,
      revision: this.revision,
      offset,
    };
  }

  // Keep the edits of the op being built until StoreController releases or rolls them back
  begin(): void {
    if (this.pending) throw new Error("A Store op is already pending");
    this.db.exec("SAVEPOINT pending");
    this.pending = true;
  }

  // Check column types and write one edit or batch, inserts get temporary negative IDs when Python assigns IDs
  applyEdit(id: string, operation: DataFrameEdit, temporaryIds: boolean): DataFrameEditResult {
    if (!this.pending) throw new Error("DataFrame edits require a Store op");
    const resource = this.resource(id);
    const table = quote(resource.table);
    const identities = [];
    this.db.exec("SAVEPOINT dataframe_edit");
    try {
      for (const edit of operation.kind === "batch" ? operation.edits : [operation]) {
        const kind = edit.kind ?? "update";
        let rowId = edit.row_identity;
        if (rowId === undefined && edit.identity) {
          const entries = Object.entries(edit.identity);
          const binds = entries.map(([name, value]) => {
            const field = this.field(resource, name);
            return encodeSqlValue(parseCellValue(value, field), field.type);
          });
          const where = entries.map(([name]) => `${quote(name)} IS ?`).join(" AND ");
          const rows = this.db.selectArrays(`SELECT ${quote(identity)} FROM ${table} WHERE ${where}`, binds);
          if (rows.length !== 1) throw new Error("An edit identity must select exactly one row");
          rowId = rows[0][0];
        }
        if (kind !== "insert" && !this.db.selectValue(`SELECT count(*) FROM ${table} WHERE ${quote(identity)}=?`, [rowId]))
          throw new Error("The edited row no longer exists");
        if (kind === "delete") {
          this.db.exec({ sql: `DELETE FROM ${table} WHERE ${quote(identity)}=?`, bind: [rowId] });
        } else {
          const submitted = edit.column ? { [edit.column]: edit.value } : edit.values;
          if (!submitted || identity in submitted) throw new Error("Edit values must contain data columns only");
          let columns = kind === "insert" ? resource.fields.map((field) => field.name) : Object.keys(submitted);
          const values = columns.map((name) => {
            const field = this.field(resource, name);
            return encodeSqlValue(parseCellValue(submitted[name], field), field.type);
          });
          if (kind === "insert") {
            if (temporaryIds) {
              columns = [identity, ...columns];
              values.unshift(this.temporaryIdentity--);
            }
            this.db.exec({
              sql: `INSERT INTO ${table} (${columns.map(quote).join(",")}) VALUES(${columns.map(() => "?").join(",")})`,
              bind: values,
            });
            rowId = this.db.selectValue("SELECT last_insert_rowid()");
          } else if (kind === "update") {
            if (columns.length)
              this.db.exec({
                sql: `UPDATE ${table} SET ${columns.map((name) => `${quote(name)}=?`).join(",")} WHERE ${quote(identity)}=?`,
                bind: [...values, rowId],
              });
          } else throw new Error(`Unknown DataFrame edit ${kind}`);
        }
        identities.push(Number(rowId));
      }
      this.db.exec("RELEASE dataframe_edit");
      return { status: "applied", revision: this.revision, row_identity: identities[0], row_identities: identities };
    } catch (error) {
      this.db.exec("ROLLBACK TO dataframe_edit");
      this.db.exec("RELEASE dataframe_edit");
      throw error;
    }
  }

  // Keep the pending edits, used by browser only Stores where no Python process decides
  release(): void {
    if (this.pending) this.db.exec("RELEASE pending");
    this.pending = false;
  }

  // Remove the pending edits, before applying the rows Python accepted or after a rejection
  rollback(): void {
    if (this.pending) {
      this.db.exec("ROLLBACK TO pending");
      this.db.exec("RELEASE pending");
    }
    this.pending = false;
  }

  // Write the row changes of commits accepted by Python
  applyCommits(commits: any[], bytes: Uint8Array): void {
    this.db.transaction(() => {
      for (const commit of commits) {
        for (const change of commit.changes) {
          const resource = this.resource(change.resource_id);
          const table = arrow.tableFromIPC(bytes.subarray(change.offset, change.offset + change.length));
          const fields = table.schema.fields;
          const names = fields.map((field) => field.name);
          const sql =
            change.kind === "delete"
              ? `DELETE FROM ${quote(resource.table)} WHERE ${quote(identity)}=?`
              : change.kind === "insert"
              ? `INSERT INTO ${quote(resource.table)} (${names.map(quote).join(",")}) VALUES(${names
                  .map(() => "?")
                  .join(",")})`
              : `UPDATE ${quote(resource.table)} SET ${names
                  .slice(1)
                  .map((name) => `${quote(name)}=?`)
                  .join(",")} WHERE ${quote(identity)}=?`;
          const statement = this.db.prepare(sql);
          const columns = fields.map((field) => table.getChild(field.name));
          try {
            for (let idx = 0; idx < table.numRows; idx++) {
              const values = fields.map((field, pos) => encodeSqlValue(arrowValue(columns[pos], idx), field.type));
              if (change.kind === "update") values.push(values.shift());
              statement.bind(values).stepReset();
            }
          } finally {
            statement.finalize();
          }
        }
      }
    });
  }

  hasResource(id: string): boolean {
    return this.resources.has(id);
  }

  close(): void {
    this.db.close();
  }
}

// Load SQLite for local tables, using the WASM file included in the application build
export async function createBrowserDatabase(resources: BrowserResource[]): Promise<BrowserDatabase> {
  if (!sqlitePromise)
    sqlitePromise = import("@sqlite.org/sqlite-wasm").then(({ default: initialize }) =>
      (initialize as any)({
        locateFile: () => new URL("@sqlite.org/sqlite-wasm/sqlite3.wasm", import.meta.url).href,
      })
    );
  return new BrowserDatabase(await sqlitePromise, resources);
}
