"""v1: Schema discovery and SQL execution tools.

In the v0 baseline the agent's only tool was submit_answer, so it hallucinated table
names (44-49/64 SQL errors) or "explored" by submitting information_schema queries as
its final answer (4-15/64). These tools let it discover structure and test queries
before submitting.

Everything is discovered from the live database via information_schema, with no
hand-maintained schema map, so the same tools work on any DuckDB warehouse.
"""

from typing import Any

import duckdb
import polars as pl

from framework.agent import Tool
from framework.database import DATABASE_PATH
from tools.guide_tools import guides_for_schema, read_guide

# Output budgets: keep tool results small enough that many calls fit in context.
_PREVIEW_ROWS = 20
_SAMPLE_ROWS = 3
_MAX_CELL_CHARS = 80
_MAX_TABLES_PER_DESCRIBE = 10
_SYSTEM_SCHEMAS = ("information_schema", "pg_catalog")


def _connect() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(DATABASE_PATH), read_only=True)


def _fmt_cell(value: Any) -> str:
    """Render a cell compactly. Truncate long values and hide binary blobs."""
    if value is None:
        return "NULL"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "<binary>"
    if isinstance(value, str) and not value.strip():
        # Make '' and whitespace-only strings visible; they differ from NULL in SQL.
        return repr(value)
    text = str(value).replace("\n", "\\n")
    if len(text) > _MAX_CELL_CHARS:
        text = text[: _MAX_CELL_CHARS - 3] + "..."
    return text


def _fmt_rows(df: pl.DataFrame, max_rows: int) -> str:
    """Render a dataframe as a pipe-separated table, keeping every column visible."""
    header = " | ".join(df.columns)
    lines = [header, "-" * min(len(header), 120)]
    for row in df.head(max_rows).iter_rows():
        lines.append(" | ".join(_fmt_cell(v) for v in row))
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# list_schemas
# -----------------------------------------------------------------------------


def list_schemas() -> str:
    """List every schema with its table names."""
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT table_schema, list(table_name ORDER BY table_name)
            FROM information_schema.tables
            WHERE NOT list_contains(?, table_schema)
            GROUP BY table_schema
            ORDER BY table_schema
            """,
            [list(_SYSTEM_SCHEMAS)],
        ).fetchall()
    finally:
        conn.close()
    lines = []
    for schema, tables in rows:
        # v3b: mark schemas that have a linked business-rule guide (cheap: a few tokens),
        # which helps pick the right one among look-alike schemas.
        linked = guides_for_schema(schema)
        tag = f"  [guide: {', '.join(linked)}]" if linked else ""
        lines.append(f"{schema}: {', '.join(tables)}{tag}")
    return (
        f"{len(rows)} schemas (format 'schema: table, table, ...  [guide: linked guide]'). "
        "Reference tables as schema.table; identifiers are case-sensitive when quoted.\n"
        + "\n".join(lines)
    )


LIST_SCHEMAS: Tool = Tool(
    name="list_schemas",
    description=(
        "List all database schemas and the tables in each. Call this first to find "
        "which schema and tables are relevant to the question."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    function=list_schemas,
)


# -----------------------------------------------------------------------------
# describe_table
# -----------------------------------------------------------------------------


def _describe_one(conn: duckdb.DuckDBPyConnection, qualified: str) -> str:
    if "." not in qualified:
        return f"## {qualified}\nError: use the 'schema.table' form."
    schema, table = qualified.split(".", 1)
    schema, table = schema.strip().strip('"'), table.strip().strip('"')
    cols = conn.execute(
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = ? AND table_name = ?
        ORDER BY ordinal_position
        """,
        [schema, table],
    ).fetchall()
    if not cols:
        return f"## {schema}.{table}\nError: table not found. Check list_schemas."

    ident = f'"{schema}"."{table}"'
    row_count = conn.execute(f"SELECT COUNT(*) FROM {ident}").fetchone()
    n_rows = row_count[0] if row_count else 0
    col_lines = [
        f"- {name} ({dtype}{', nullable' if nullable == 'YES' else ''})"
        for name, dtype, nullable in cols
    ]
    sample = pl.DataFrame(conn.execute(f"SELECT * FROM {ident} LIMIT {_SAMPLE_ROWS}").arrow())
    return (
        f"## {schema}.{table} ({n_rows} rows)\n"
        "Columns:\n" + "\n".join(col_lines) + "\n"
        f"Sample rows:\n{_fmt_rows(sample, _SAMPLE_ROWS)}"
    )


def describe_table(tables: list[str]) -> str:
    """Describe one or more tables: columns, types, row count, sample rows."""
    if isinstance(tables, str):  # tolerate a bare string from the model
        tables = [tables]
    if not tables:
        return "Error: provide at least one 'schema.table'."
    notes = ""
    if len(tables) > _MAX_TABLES_PER_DESCRIBE:
        notes = f"(Only the first {_MAX_TABLES_PER_DESCRIBE} tables were described.)\n\n"
        tables = tables[:_MAX_TABLES_PER_DESCRIBE]
    conn = _connect()
    try:
        parts = []
        for t in tables:
            try:
                parts.append(_describe_one(conn, t))
            except duckdb.Error as e:
                parts.append(f"## {t}\nDuckDB error: {e}")
    finally:
        conn.close()
    return notes + "\n\n".join(parts) + _linked_guides_section(tables)


def _linked_guides_section(tables: list[str]) -> str:
    """v3b: attach the full text of guides linked to the described schemas.

    In v3a, 3 of 7 hard regressions never consulted any guide in that run, although
    they passed when they did. Whether the agent reads the guide was stochastic.
    describe_table is called in nearly every episode right after choosing a schema, so
    attaching the linked guide here makes guide exposure structural, not optional.
    """
    schemas = []
    for t in tables:
        schema = t.split(".", 1)[0].strip().strip('"') if "." in t else ""
        if schema and schema not in schemas:
            schemas.append(schema)
    names: list[str] = []
    for schema in schemas:
        for name in guides_for_schema(schema):
            if name not in names:
                names.append(name)
    if not names:
        return ""
    return (
        "\n\n---\nBusiness-rule guide(s) linked to "
        f"{', '.join(schemas)} (auto-attached; these definitions are authoritative, "
        "apply the ones relevant to the question):\n\n" + read_guide(names)
    )


DESCRIBE_TABLE: Tool = Tool(
    name="describe_table",
    description=(
        "Describe one or more tables: column names and types, row count, and a few "
        "sample rows (useful for seeing how values are actually encoded). Pass several "
        "tables at once to save steps. Also attaches the business-rule guide linked to "
        "the tables' schema, if any."
    ),
    parameters={
        "type": "object",
        "properties": {
            "tables": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Schema-qualified table names, e.g. ['financial.loan', 'financial.account']."
                ),
            },
        },
        "required": ["tables"],
    },
    function=describe_table,
)


# -----------------------------------------------------------------------------
# run_sql
# -----------------------------------------------------------------------------


def run_sql(query: str) -> str:
    """Execute a read-only query and return the row count, column types, and a preview."""
    conn = _connect()
    try:
        result = conn.execute(query)
        df = pl.DataFrame(result.fetch_arrow_table())
    except duckdb.Error as e:
        return f"DuckDB error: {e}"
    except Exception as e:
        return f"Error: {e}"
    finally:
        conn.close()

    types = ", ".join(f"{c}: {t}" for c, t in zip(df.columns, df.dtypes, strict=True))
    shown = min(df.height, _PREVIEW_ROWS)
    suffix = f" (showing first {shown})" if df.height > shown else ""
    return (
        f"{df.height} rows x {df.width} columns{suffix}\n"
        f"Column types: {types}\n"
        f"{_fmt_rows(df, _PREVIEW_ROWS)}"
    )


RUN_SQL: Tool = Tool(
    name="run_sql",
    description=(
        "Execute a read-only DuckDB SQL query and see the result: row count, column "
        "types, and up to 20 rows. Use it to explore data (distinct values, NULLs, "
        "date ranges) and to test your final query before submitting it. This does NOT "
        "submit an answer."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "DuckDB SQL with schema-qualified table names (schema.table).",
            },
        },
        "required": ["query"],
    },
    function=run_sql,
)


DATABASE_TOOLS: list[Tool] = [LIST_SCHEMAS, DESCRIBE_TABLE, RUN_SQL]
