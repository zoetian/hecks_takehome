"""Debug tool for eval runs: diagnose why each case failed, drill into one case, track history.

Reads the trace files written by `uv run evaluate` (logs/run_*/evals_{easy,hard}/*.json),
re-executes the gold and submitted SQL (traces do not store dataframes), and classifies
each failure. It never changes evaluate.py or the agent; it is a read-only debugging aid.

Usage:
    uv run python scripts/debug_eval.py summary [RUN_DIR] [--split hard] [--label v1]
    uv run python scripts/debug_eval.py case    [RUN_DIR] CASE [--split hard] [--thinking]
    uv run python scripts/debug_eval.py history
    uv run python scripts/debug_eval.py diff    RUN_A RUN_B [--split hard]

RUN_DIR defaults to the latest logs/run_*. CASE is the 0-based index in the eval file
(the `#` column in `summary`) or a trace-id prefix.

Every report is printed to the terminal and also saved under RUN_DIR/debug/:
    summary_<split>.txt / summary_<split>.json   (json doubles as a diagnosis cache)
    case_<split>_<NN>.txt
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import sqlglot
from rich.console import Console
from rich.markup import escape
from rich.table import Table

# Make the repo root importable when run as `python scripts/debug_eval.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evaluation.compare import _cell, _equal  # noqa: E402
from framework.database import execute_query  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
LOGS = REPO / "logs"
DATA = REPO / "evaluation" / "data"
SPLITS = ("evals_easy", "evals_hard")
TOLERANCE = Decimal("0.0001")

# =============================================================================
# Loading
# =============================================================================


def latest_run() -> Path:
    runs = sorted(p for p in LOGS.glob("run_*") if p.is_dir())
    if not runs:
        sys.exit("No runs found under logs/. Run `uv run evaluate` first.")
    return runs[-1]


def load_dataset(split: str) -> list[dict[str, str]]:
    return json.loads((DATA / f"{split}.json").read_text())


def load_traces(run_dir: Path, split: str) -> list[dict[str, Any]]:
    """Load the traces of one split, sorted by case index in the eval file."""
    split_dir = run_dir / split
    if not split_dir.is_dir():
        return []
    dataset = load_dataset(split)
    index_by_prompt = {item["prompt"]: i for i, item in enumerate(dataset)}
    traces = []
    for path in split_dir.glob("*.json"):
        t = json.loads(path.read_text())
        t["_index"] = index_by_prompt.get(t["case"]["prompt"], -1)
        traces.append(t)
    return sorted(traces, key=lambda t: t["_index"])


def missing_rules(index: int) -> str | None:
    """The text the easy prompt adds on top of the hard prompt for the same case.

    Easy and hard share questions and gold queries one-to-one by index; the easy prompt
    spells out the business rules the hard version expects the agent to find in guides.
    For debugging only; this is never shown to the agent.
    """
    easy, hard = load_dataset("evals_easy"), load_dataset("evals_hard")
    if not (0 <= index < len(easy)) or easy[index]["query"] != hard[index]["query"]:
        return None
    e, h = easy[index]["prompt"].strip(), hard[index]["prompt"].strip()
    if e.startswith(h):
        return e[len(h):].strip() or None
    return f"(prompts differ, full easy prompt) {e}"


_USAGE_RE = re.compile(r"prompt_tokens=(\d+), completion_tokens=(\d+)")


def usage_of(trace: dict[str, Any]) -> tuple[int, int]:
    """Total (prompt, completion) tokens, summed over generations (works for errors too)."""
    p = c = 0
    for e in trace["events"]:
        if e["type"] == "GENERATION_END":
            m = _USAGE_RE.search(str(e["data"].get("usage", "")))
            if m:
                p, c = p + int(m.group(1)), c + int(m.group(2))
    return p, c


def tool_calls_of(trace: dict[str, Any]) -> list[str]:
    return [e["data"]["name"] for e in trace["events"] if e["type"] == "TOOL_CALL_PARSED"]


def turns_of(trace: dict[str, Any]) -> int:
    return sum(1 for e in trace["events"] if e["type"] == "ITERATION_START")


# =============================================================================
# SQL helpers
# =============================================================================


def tables_in(sql: str | None) -> set[str]:
    """Schema-qualified tables referenced by a query (lower-cased)."""
    if not sql:
        return set()
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:
        return set()
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(sqlglot.exp.CTE)}
    out = set()
    for t in tree.find_all(sqlglot.exp.Table):
        name = t.name.lower()
        if t.db:
            out.add(f"{t.db.lower()}.{name}")
        elif name not in cte_names:
            out.add(name)
    return out


def predicates_in(sql: str | None) -> set[str]:
    """Normalized atomic predicates from every WHERE/HAVING/JOIN ON, split on AND."""
    if not sql:
        return set()
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:
        return set()
    preds: set[str] = set()

    def split_and(node: sqlglot.exp.Expression) -> None:
        if isinstance(node, sqlglot.exp.And):
            split_and(node.left)
            split_and(node.right)
        elif isinstance(node, sqlglot.exp.Paren):
            split_and(node.this)
        else:
            # Drop table qualifiers so `c.charge_amt` and `charge_amt` compare equal.
            node = node.copy()
            for col in node.find_all(sqlglot.exp.Column):
                col.set("table", None)
            preds.add(node.sql(dialect="duckdb").lower())

    for clause in (*tree.find_all(sqlglot.exp.Where), *tree.find_all(sqlglot.exp.Having)):
        split_and(clause.this)
    for join in tree.find_all(sqlglot.exp.Join):
        on = join.args.get("on")
        if on is not None:
            split_and(on)
    return preds


def pretty_sql(sql: str | None) -> str:
    if not sql:
        return "(none)"
    try:
        return sqlglot.transpile(sql, read="duckdb", pretty=True)[0]
    except Exception:
        return sql


# =============================================================================
# Diagnosis
# =============================================================================


@dataclass
class ColumnMatch:
    gold_col: str
    matched_sub_col: str | None


@dataclass
class Diagnosis:
    category: str
    detail: str = ""
    gold_shape: tuple[int, int] | None = None
    sub_shape: tuple[int, int] | None = None
    columns: list[ColumnMatch] = field(default_factory=list)
    gold_only_tables: list[str] = field(default_factory=list)
    sub_only_tables: list[str] = field(default_factory=list)
    gold_only_preds: list[str] = field(default_factory=list)
    sub_only_preds: list[str] = field(default_factory=list)


def _column_matches(gold: pl.DataFrame, sub: pl.DataFrame) -> list[ColumnMatch]:
    """For each gold column, find a submitted column with the same multiset of values.

    Uses the evaluator's own cell normalization and tolerance, so "matched" here means
    the evaluator would accept that column on its own (ignoring row association).
    """
    sub_sorted = {c: sorted(_cell(v) for v in sub[c]) for c in sub.columns}
    out = []
    for gc in gold.columns:
        g = sorted(_cell(v) for v in gold[gc])
        hit = next(
            (
                sc
                for sc, s in sub_sorted.items()
                if len(s) == len(g) and all(_equal(a, b, TOLERANCE) for a, b in zip(g, s))
            ),
            None,
        )
        out.append(ColumnMatch(gc, hit))
    return out


def diagnose(trace: dict[str, Any], with_frames: bool = False) -> tuple[Diagnosis, Any, Any]:
    """Classify one case. Returns (diagnosis, gold_df, sub_df); dfs only if with_frames."""
    r = trace["result"]
    gold_sql, sub_sql = trace["case"]["gold_query"], r.get("submitted_query")
    ft, err = r["failure_type"], r.get("error") or ""

    gt, st = tables_in(gold_sql), tables_in(sub_sql)
    gp, sp = predicates_in(gold_sql), predicates_in(sub_sql)
    d = Diagnosis(
        category="PASS" if r["passed"] else ft,
        gold_only_tables=sorted(gt - st),
        sub_only_tables=sorted(st - gt),
        gold_only_preds=sorted(gp - sp),
        sub_only_preds=sorted(sp - gp),
    )
    if r["passed"] and not with_frames:
        return d, None, None

    if ft == "AGENT_ERROR":
        d.category = "MAX_ITERATIONS" if "Max iterations" in err else "AGENT_ERROR"
        d.detail = err[:200]
        return d, None, None
    if ft in ("EXCEPTION", "INFRA_ERROR"):
        d.category = "INFRA"
        d.detail = err[:200]
        return d, None, None
    if ft == "NO_SUBMISSION":
        return d, None, None
    if ft == "SQL_ERROR":
        d.detail = err.replace("Submitted query execution failed: ", "")[:200]
        return d, None, None

    gold_res = execute_query(gold_sql)
    sub_res = execute_query(sub_sql or "")
    gold_df, sub_df = gold_res.dataframe, sub_res.dataframe
    if gold_df is None or sub_df is None:
        d.category = "INFRA"
        d.detail = f"re-execution failed: {gold_res.error_message or sub_res.error_message}"
        return d, gold_df, sub_df
    d.gold_shape, d.sub_shape = gold_df.shape, sub_df.shape
    d.columns = _column_matches(gold_df, sub_df) if gold_df.height == sub_df.height else []

    if r["passed"]:
        return d, gold_df, sub_df

    gold_schemas = {t.split(".")[0] for t in gt if "." in t}
    sub_schemas = {t.split(".")[0] for t in st if "." in t}
    if gold_schemas and sub_schemas and gold_schemas != sub_schemas:
        d.category = "WRONG_SCHEMA"
        d.detail = f"used {sorted(sub_schemas)}, gold uses {sorted(gold_schemas)}"
    elif gold_df.height != sub_df.height:
        d.category = "ROWCOUNT"
        d.detail = f"{sub_df.height} rows submitted vs {gold_df.height} expected"
    elif sub_df.width < gold_df.width:
        d.category = "MISSING_COLUMN"
        d.detail = f"{sub_df.width} cols submitted vs {gold_df.width} expected"
    else:
        bad = [c.gold_col for c in d.columns if c.matched_sub_col is None]
        if bad:
            d.category = "VALUES_DIFFER"
            d.detail = f"no matching submitted column for: {', '.join(bad)}"
        else:
            d.category = "ROW_ASSOCIATION"
            d.detail = "every column matches on its own, but values are paired differently"
    return d, gold_df, sub_df


# =============================================================================
# Rendering helpers
# =============================================================================

CATEGORY_STYLE = {
    "PASS": "green",
    "WRONG_SCHEMA": "magenta",
    "ROWCOUNT": "yellow",
    "MISSING_COLUMN": "yellow",
    "VALUES_DIFFER": "yellow",
    "ROW_ASSOCIATION": "yellow",
    "SQL_ERROR": "red",
    "MAX_ITERATIONS": "red",
    "INFRA": "red",
}

CATEGORY_HELP = {
    "WRONG_SCHEMA": "picked a different schema than the gold query (look-alike databases)",
    "ROWCOUNT": "wrong filter / grouping / LIMIT / DISTINCT: row count differs",
    "MISSING_COLUMN": "fewer columns than required",
    "VALUES_DIFFER": "right shape, but some column's values differ (definition or rounding)",
    "ROW_ASSOCIATION": "columns individually right but paired differently across rows",
    "SQL_ERROR": "submitted SQL fails to execute (submitted without testing?)",
    "MAX_ITERATIONS": "agent never submitted within the iteration budget",
    "NO_SUBMISSION": "agent stopped without calling submit_answer",
    "INFRA": "API/provider error or harness failure",
}


def make_console() -> Console:
    return Console(record=True, width=160)


def save(console: Console, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(console.export_text())
    console.print(f"\n[dim]Saved to {path.relative_to(REPO)}[/dim]")


def _short(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _row_strings(df: pl.DataFrame, cols: list[str]) -> list[tuple[str, ...]]:
    def fmt(v: Any) -> str:
        if isinstance(v, float):
            return f"{v:.6g}"
        return str(v)

    return [tuple(fmt(v) for v in row) for row in df.select(cols).iter_rows()]


# =============================================================================
# summary
# =============================================================================


def cmd_summary(run_dir: Path, splits: list[str], label: str | None) -> None:
    if label:
        (run_dir / "label.txt").write_text(label + "\n")
    for split in splits:
        traces = load_traces(run_dir, split)
        if not traces:
            continue
        console = make_console()
        cache_path = run_dir / "debug" / f"summary_{split}.json"
        cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

        rows = []
        for t in traces:
            tid = t["trace_id"]
            if tid in cache:
                diag = cache[tid]
            else:
                d, _, _ = diagnose(t)
                diag = {"category": d.category, "detail": d.detail}
                cache[tid] = diag
            rows.append((t, diag))
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, indent=2))

        n = len(traces)
        passed = sum(1 for t in traces if t["result"]["passed"])
        p_tok = sum(usage_of(t)[0] for t in traces)
        c_tok = sum(usage_of(t)[1] for t in traces)
        avg_turns = sum(turns_of(t) for t in traces) / n
        label_txt = (run_dir / "label.txt").read_text().strip() if (
            run_dir / "label.txt"
        ).exists() else "-"

        console.rule(f"[bold]{run_dir.name} / {split}  (label: {label_txt})")
        console.print(
            f"[bold]{passed}/{n} passed ({passed / n:.1%})[/bold]   "
            f"tokens {p_tok / 1e6:.2f}M in / {c_tok / 1e3:.0f}k out   "
            f"avg {avg_turns:.1f} turns/case"
        )

        cats = Counter(d["category"] for _, d in rows if d["category"] != "PASS")
        if cats:
            console.print("\n[bold]Failures by diagnosis[/bold]")
            for cat, k in cats.most_common():
                style = CATEGORY_STYLE.get(cat, "white")
                help_txt = CATEGORY_HELP.get(cat, "")
                console.print(f"  [{style}]{cat:<16}[/{style}] {k:>3}   [dim]{help_txt}[/dim]")

        table = Table(show_lines=False, header_style="bold", expand=True)
        table.add_column("#", justify="right", width=3)
        table.add_column("diagnosis", width=16)
        table.add_column("detail", ratio=2, overflow="fold")
        table.add_column("turns", justify="right", width=5)
        table.add_column("L/D/S", justify="right", width=8)
        table.add_column("question", ratio=3, overflow="ellipsis", no_wrap=True)
        for t, d in rows:
            if d["category"] == "PASS":
                continue
            tc = Counter(tool_calls_of(t))
            style = CATEGORY_STYLE.get(d["category"], "white")
            table.add_row(
                str(t["_index"]),
                f"[{style}]{d['category']}[/{style}]",
                escape(_short(d["detail"], 120)),
                str(turns_of(t)),
                f"{tc['list_schemas']}/{tc['describe_table']}/{tc['run_sql']}",
                escape(t["case"]["prompt"]),
            )
        console.print()
        console.print(table)
        passed_ids = [str(t["_index"]) for t, d in rows if d["category"] == "PASS"]
        console.print(f"[green]Passed:[/green] {', '.join(passed_ids) or '-'}")
        console.print(
            "[dim]L/D/S = list_schemas / describe_table / run_sql calls. "
            f"Drill in: uv run python scripts/debug_eval.py case {run_dir.relative_to(REPO)} "
            f"<#> --split {split.removeprefix('evals_')}[/dim]"
        )
        save(console, run_dir / "debug" / f"summary_{split}.txt")


# =============================================================================
# case
# =============================================================================


def _find_case(traces: list[dict[str, Any]], key: str) -> dict[str, Any]:
    if key.isdigit():
        for t in traces:
            if t["_index"] == int(key):
                return t
    for t in traces:
        if t["trace_id"].startswith(key):
            return t
    sys.exit(f"Case {key!r} not found.")


def _thinking_by_turn(trace: dict[str, Any]) -> dict[int, str]:
    out: dict[int, str] = {}
    turn = 0
    for e in trace["events"]:
        if e["type"] == "ITERATION_START":
            turn = e["data"].get("iteration", turn + 1)
        elif e["type"] == "THINKING_CHUNK":
            out[turn] = out.get(turn, "") + e["data"].get("chunk", "")
    return out


def cmd_case(run_dir: Path, split: str, key: str, show_thinking: bool) -> None:
    traces = load_traces(run_dir, split)
    t = _find_case(traces, key)
    r = t["result"]
    console = make_console()
    d, gold_df, sub_df = diagnose(t, with_frames=True)
    style = CATEGORY_STYLE.get(d.category, "white")

    console.rule(f"[bold]{split} #{t['_index']}  ({t['trace_id'][:8]})  [{style}]{d.category}")

    # 1. Question + what the agent was supposed to find
    console.print("\n[bold cyan]1. Question[/bold cyan]")
    console.print(escape(t["case"]["prompt"]))
    if split == "evals_hard":
        rules = missing_rules(t["_index"])
        if rules:
            console.print("\n[bold cyan]   Rules the agent had to find in the guides[/bold cyan] "
                          "[dim](= extra text in the easy prompt; debug-only)[/dim]")
            console.print(f"[yellow]{escape(rules)}[/yellow]")

    # 2. Diagnosis
    console.print("\n[bold cyan]2. Diagnosis[/bold cyan]")
    console.print(f"[{style}]{d.category}[/{style}]  {escape(d.detail)}")
    if d.category in CATEGORY_HELP:
        console.print(f"[dim]{CATEGORY_HELP[d.category]}[/dim]")
    if d.gold_only_tables or d.sub_only_tables:
        console.print(f"  tables only in gold:      {d.gold_only_tables or '-'}")
        console.print(f"  tables only in submitted: {d.sub_only_tables or '-'}")
    if d.gold_only_preds or d.sub_only_preds:
        console.print("  [bold]filter/join conditions only in gold[/bold] (agent missed these):")
        for p in d.gold_only_preds or ["-"]:
            console.print(f"    [green]+ {escape(p)}[/green]")
        console.print("  [bold]conditions only in submitted[/bold] (agent added these):")
        for p in d.sub_only_preds or ["-"]:
            console.print(f"    [red]- {escape(p)}[/red]")

    # 3. SQL side by side
    console.print("\n[bold cyan]3. SQL[/bold cyan]")
    sql_table = Table(show_header=True, header_style="bold", expand=True, show_lines=False)
    sql_table.add_column("[green]gold[/green]", ratio=1, overflow="fold")
    sql_table.add_column("[red]submitted[/red]", ratio=1, overflow="fold")
    sql_table.add_row(escape(pretty_sql(t["case"]["gold_query"])),
                      escape(pretty_sql(r.get("submitted_query"))))
    console.print(sql_table)

    # 4. Results
    console.print("\n[bold cyan]4. Results[/bold cyan]")
    if gold_df is None or sub_df is None:
        console.print("[dim](no result to compare)[/dim]")
    else:
        console.print(f"gold {gold_df.shape[0]} rows x {gold_df.shape[1]} cols   |   "
                      f"submitted {sub_df.shape[0]} rows x {sub_df.shape[1]} cols")
        if d.columns:
            console.print("Column-by-column (does a submitted column hold the same values?):")
            for c in d.columns:
                if c.matched_sub_col:
                    console.print(f"  [green]✓[/green] {c.gold_col}  ←  {c.matched_sub_col}")
                else:
                    console.print(f"  [red]✗[/red] {c.gold_col}  (no submitted column matches)")
        _render_row_diff(console, gold_df, sub_df, d)

    # 5. Trajectory
    console.print("\n[bold cyan]5. Agent trajectory[/bold cyan]")
    p_tok, c_tok = usage_of(t)
    console.print(f"[dim]{turns_of(t)} turns, {p_tok:,} prompt / {c_tok:,} completion tokens, "
                  f"{t.get('duration_seconds') or 0:.0f}s[/dim]")
    thinking = _thinking_by_turn(t) if show_thinking else {}
    turn, step = 0, 0
    pending: dict[str, Any] | None = None
    for e in t["events"]:
        if e["type"] == "ITERATION_START":
            turn = e["data"].get("iteration", turn + 1)
            if turn in thinking:
                console.print(f"  [dim italic]turn {turn} thinking: "
                              f"{escape(_short(thinking[turn], 600))}[/dim italic]")
        elif e["type"] == "TOOL_CALL_PARSED":
            pending = e["data"]
        elif e["type"] == "TOOL_EXECUTION_END" and pending is not None:
            step += 1
            name = pending["name"]
            args = pending.get("arguments", {})
            arg_txt = args.get("query") or args.get("tables") or args.get("name") or args
            result = str(e["data"].get("result", ""))
            ok = not result.startswith(("DuckDB error", "Error"))
            mark = "[green]✓[/green]" if ok else "[red]✗[/red]"
            console.print(f"  {step:>2}. [bold]{name}[/bold] {mark} "
                          f"{escape(_short(str(arg_txt), 220))}")
            if name != "list_schemas":
                console.print(f"      [dim]→ {escape(_short(result, 240))}[/dim]")
            pending = None
    if not t["events"]:
        console.print("[dim](no events: the agent raised an exception, which evaluate.py "
                      "records without events)[/dim]")

    out = run_dir / "debug" / f"case_{split}_{t['_index']:02d}.txt"
    save(console, out)


def _render_row_diff(console: Console, gold: pl.DataFrame, sub: pl.DataFrame,
                     d: Diagnosis, k: int = 5) -> None:
    """Show rows present in one result but not the other, over the matched columns."""
    # Use the value-based column mapping where we have one, and pair the remaining
    # (mismatched) gold columns with the remaining submitted columns by position. The
    # mismatched columns are exactly the ones we need to see.
    matched = {c.gold_col: c.matched_sub_col for c in d.columns if c.matched_sub_col}
    spare_sub = [c for c in sub.columns if c not in set(matched.values())]
    pairs: list[tuple[str, str]] = []
    for gc in gold.columns:
        if gc in matched:
            pairs.append((gc, matched[gc]))
        elif spare_sub:
            pairs.append((gc, spare_sub.pop(0)))
    gcols = [g for g, _ in pairs]
    scols = [s for _, s in pairs]
    try:
        g_rows = Counter(_row_strings(gold, gcols))
        s_rows = Counter(_row_strings(sub, scols))
    except Exception as e:  # dtype oddities shouldn't kill the report
        console.print(f"[dim](row diff unavailable: {e})[/dim]")
        return
    only_g = list((g_rows - s_rows).elements())
    only_s = list((s_rows - g_rows).elements())
    if not only_g and not only_s:
        console.print("[dim]Row sets are identical as strings; difference is below display "
                      "precision or in column pairing.[/dim]")
        return
    table = Table(title=f"Rows only in one side (columns: {', '.join(gcols)})",
                  header_style="bold", expand=True)
    table.add_column(f"[green]only in gold ({len(only_g)})[/green]", ratio=1, overflow="fold")
    table.add_column(f"[red]only in submitted ({len(only_s)})[/red]", ratio=1, overflow="fold")
    for i in range(min(k, max(len(only_g), len(only_s)))):
        table.add_row(escape(" | ".join(only_g[i])) if i < len(only_g) else "",
                      escape(" | ".join(only_s[i])) if i < len(only_s) else "")
    console.print(table)


# =============================================================================
# history / diff
# =============================================================================


def cmd_history() -> None:
    console = make_console()
    table = Table(title="Eval run history", header_style="bold")
    for col in ("run", "label", "easy", "hard", "tokens in/out"):
        table.add_column(col)
    for run_dir in sorted(p for p in LOGS.glob("run_*") if p.is_dir()):
        cells, p_tot, c_tot = [], 0, 0
        for split in SPLITS:
            traces = load_traces(run_dir, split)
            if not traces:
                cells.append("-")
                continue
            passed = sum(1 for t in traces if t["result"]["passed"])
            cells.append(f"{passed}/{len(traces)} ({passed / len(traces):.0%})")
            for t in traces:
                p, c = usage_of(t)
                p_tot, c_tot = p_tot + p, c_tot + c
        label = (run_dir / "label.txt").read_text().strip() if (
            run_dir / "label.txt"
        ).exists() else ""
        table.add_row(run_dir.name, label, *cells, f"{p_tot / 1e6:.2f}M / {c_tot / 1e3:.0f}k")
    console.print(table)
    console.print("[dim]Label a run with: summary RUN_DIR --label v1[/dim]")
    save(console, LOGS / "history.txt")


def _diag_map(run_dir: Path, split: str) -> dict[int, tuple[bool, str]]:
    cache_path = run_dir / "debug" / f"summary_{split}.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    out = {}
    for t in load_traces(run_dir, split):
        cat = cache.get(t["trace_id"], {}).get("category")
        if cat is None:
            cat = "PASS" if t["result"]["passed"] else t["result"]["failure_type"]
        out[t["_index"]] = (t["result"]["passed"], cat)
    return out


def cmd_diff(run_a: Path, run_b: Path, splits: list[str]) -> None:
    console = make_console()
    for split in splits:
        a, b = _diag_map(run_a, split), _diag_map(run_b, split)
        if not a or not b:
            continue
        prompts = {i: item["prompt"] for i, item in enumerate(load_dataset(split))}
        console.rule(f"[bold]{split}: {run_a.name} → {run_b.name}")
        pa, pb = sum(v[0] for v in a.values()), sum(v[0] for v in b.values())
        console.print(f"passed {pa} → {pb}  ({pb - pa:+d})")
        groups = [
            ("FIXED (fail → pass)", "green", lambda x, y: not x and y),
            ("REGRESSED (pass → fail)", "red", lambda x, y: x and not y),
            ("STILL FAILING", "yellow", lambda x, y: not x and not y),
        ]
        for title, style, cond in groups:
            ids = [i for i in sorted(a) if i in b and cond(a[i][0], b[i][0])]
            console.print(f"\n[bold {style}]{title}: {len(ids)}[/bold {style}]")
            for i in ids:
                console.print(f"  #{i:<3} {a[i][1]:<15} → {b[i][1]:<15} "
                              f"{escape(_short(prompts.get(i, ''), 100))}")
    save(console, run_b / "debug" / f"diff_vs_{run_a.name}.txt")


# =============================================================================
# CLI
# =============================================================================


def _resolve_run(arg: str | None) -> Path:
    if not arg:
        return latest_run()
    p = Path(arg)
    if not p.is_absolute():
        p = (REPO / p) if (REPO / p).exists() else (LOGS / arg)
    if p.name in SPLITS:  # allow passing logs/run_X/evals_hard
        p = p.parent
    if not p.is_dir():
        sys.exit(f"Run directory not found: {arg}")
    return p


def _splits(arg: str) -> list[str]:
    return list(SPLITS) if arg == "both" else [f"evals_{arg}"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("summary", help="diagnose every case of a run")
    s.add_argument("run", nargs="?")
    s.add_argument("--split", choices=["easy", "hard", "both"], default="both")
    s.add_argument("--label", help="tag this run (e.g. v1); shown in history")

    c = sub.add_parser("case", help="drill into one case")
    c.add_argument("args", nargs="+", help="[RUN_DIR] CASE")
    c.add_argument("--split", choices=["easy", "hard"], default="hard")
    c.add_argument("--thinking", action="store_true", help="show model reasoning per turn")

    sub.add_parser("history", help="pass rates of every run under logs/")

    dfp = sub.add_parser("diff", help="compare two runs case by case")
    dfp.add_argument("run_a")
    dfp.add_argument("run_b")
    dfp.add_argument("--split", choices=["easy", "hard", "both"], default="both")

    a = ap.parse_args()
    if a.cmd == "summary":
        cmd_summary(_resolve_run(a.run), _splits(a.split), a.label)
    elif a.cmd == "case":
        run, key = (a.args[0], a.args[1]) if len(a.args) > 1 else (None, a.args[0])
        cmd_case(_resolve_run(run), f"evals_{a.split}", key, a.thinking)
    elif a.cmd == "history":
        cmd_history()
    elif a.cmd == "diff":
        cmd_diff(_resolve_run(a.run_a), _resolve_run(a.run_b), _splits(a.split))


if __name__ == "__main__":
    main()

