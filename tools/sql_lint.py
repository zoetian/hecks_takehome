"""v5: Pre-submit lint for silent-wrong-answer SQL patterns.

These are queries that run fine but quietly return the wrong rows. They are generic SQL
semantics, not specific to this warehouse:

- NULL-dropping comparisons: `col <> 'x'`, `col != 'x'`, `col NOT IN (...)` evaluate to
  NULL (not TRUE) when col is NULL, so those rows silently disappear. A prompt rule
  about this existed since v3a, but the agent still wrote `charge_code <> 'RF'` (v4 run 2,
  hard #56). We only flag a column if it actually contains NULLs in the database, to
  keep false positives low.

The lint returns warnings. submit_answer asks the agent to confirm or fix; it does not
block, because sometimes dropping NULLs is intended.
"""

from __future__ import annotations

import duckdb
import sqlglot
from sqlglot import exp

from framework.database import DATABASE_PATH


def _table_aliases(tree: exp.Expression) -> tuple[dict[str, tuple[str, str]], set[str]]:
    """Map alias/name -> (schema, table) for real tables, plus the set of CTE names."""
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    aliases: dict[str, tuple[str, str]] = {}
    for t in tree.find_all(exp.Table):
        if not t.db or t.name.lower() in ctes:
            continue
        ref = (t.db, t.name)
        aliases[t.name.lower()] = ref
        if t.alias:
            aliases[t.alias.lower()] = ref
    return aliases, ctes


def _null_count(conn: duckdb.DuckDBPyConnection, schema: str, table: str, column: str) -> int:
    try:
        row = conn.execute(
            f'SELECT COUNT(*) FROM "{schema}"."{table}" WHERE "{column}" IS NULL'
        ).fetchone()
        return int(row[0]) if row else 0
    except duckdb.Error:
        return 0


def _candidate_tables(
    conn: duckdb.DuckDBPyConnection, col: exp.Column, aliases: dict[str, tuple[str, str]]
) -> list[tuple[str, str]]:
    if col.table:
        ref = aliases.get(col.table.lower())
        return [ref] if ref else []
    # Unqualified column: every referenced table that has a column with this name.
    out = []
    for schema, table in set(aliases.values()):
        hit = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? AND lower(column_name) = lower(?)",
            [schema, table, col.name],
        ).fetchone()
        if hit:
            out.append((schema, table))
    return out


def lint_null_comparisons(query: str) -> list[str]:
    """Warn about <>, != and NOT IN against a literal on columns that contain NULLs."""
    try:
        tree = sqlglot.parse_one(query, read="duckdb")
    except Exception:
        return []
    aliases, _ = _table_aliases(tree)
    if not aliases:
        return []

    checks: list[tuple[exp.Column, str]] = []
    for node in tree.find_all(exp.NEQ):
        left, right = node.left, node.right
        if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
            checks.append((left, node.sql(dialect="duckdb")))
        elif isinstance(right, exp.Column) and isinstance(left, exp.Literal):
            checks.append((right, node.sql(dialect="duckdb")))
    for node in tree.find_all(exp.Not):
        inner = node.this
        if isinstance(inner, exp.In) and isinstance(inner.this, exp.Column):
            checks.append((inner.this, node.sql(dialect="duckdb")))

    warnings: list[str] = []
    seen: set[str] = set()
    conn = duckdb.connect(str(DATABASE_PATH), read_only=True)
    try:
        for col, text in checks:
            if text in seen:
                continue
            seen.add(text)
            for schema, table in _candidate_tables(conn, col, aliases):
                n = _null_count(conn, schema, table, col.name)
                if n:
                    warnings.append(
                        f"`{text}` silently drops the {n:,} rows where {schema}.{table}."
                        f"{col.name} IS NULL (NULL <> x is not TRUE). If those rows should "
                        f"be kept, use `{col.name} IS DISTINCT FROM ...` (or add "
                        f"`OR {col.name} IS NULL`)."
                    )
                    break
    finally:
        conn.close()
    return warnings
