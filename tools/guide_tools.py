"""v2: Business-rule guide tools.

In v1, 41 of 44 hard failures used the correct schema but the wrong business definitions
(what counts as "completed", "performing", "legacy", ...). Those definitions live in the
customer's markdown guides, which the agent did not know existed.

- The guide INDEX (file name, title, section headings) is put in the system prompt via
  `Tool.prompt_context`, so the agent always knows which guides exist. In v1 nothing
  prompted the model to look for rules, so a tool-only `list_guides` would rely on the
  model deciding to call it.
- `read_guide` returns full guide text. Guides are short (~400-1,200 tokens), and the
  decisive definitions often sit in a trailing section ("Aggregation definitions",
  "Scope and available data") that snippet retrieval could miss.
- `search_guides` is a keyword search across all guides, for terms defined in a guide
  whose title does not name the schema.

Everything is derived from whatever markdown files are in the guides directory; nothing
is specific to these guides. Override the location with HECKS_GUIDES_DIR.
"""

import functools
import math
import os
import re
from pathlib import Path

from framework.agent import Tool

GUIDES_DIR = Path(
    os.environ.get(
        "HECKS_GUIDES_DIR",
        Path(__file__).resolve().parent.parent / "evaluation" / "data" / "guides",
    )
)
_MAX_SEARCH_HITS = 15
_FULL_GUIDES_IN_SEARCH = 2  # v3a: full text of the top-N guides is appended to results
_MAX_GUIDES_PER_READ = 4


def _guide_files() -> dict[str, Path]:
    """Guide name (file stem) -> path, for every markdown file in the guides dir."""
    if not GUIDES_DIR.is_dir():
        return {}
    return {p.stem: p for p in sorted(GUIDES_DIR.glob("*.md"))}


def _title_and_sections(text: str) -> tuple[str, list[str]]:
    title = ""
    sections: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^(#{1,3})\s+(.*\S)\s*$", line)
        if not m:
            continue
        if len(m.group(1)) == 1 and not title:
            title = m.group(2)
        elif len(m.group(1)) > 1:
            sections.append(m.group(2))
    return title, sections


@functools.cache
def guides_for_schema(schema: str) -> tuple[str, ...]:
    """Guides linked to a database schema, inferred from the guide text itself.

    v3b: a guide is linked if its title names the schema (e.g. "(Credit Database)"), or
    its body references a schema-qualified table (e.g. "Credit.charge"). On this
    warehouse this links all 12 schemas used by the evals to exactly their guide, with
    no false links, and it needs no hand-maintained mapping.
    """
    pattern_title = re.compile(rf"\b{re.escape(schema)}\b", re.IGNORECASE)
    pattern_table = re.compile(rf"\b{re.escape(schema)}\.[A-Za-z_]", re.IGNORECASE)
    linked = []
    for name, path in _guide_files().items():
        text = path.read_text()
        title = text.splitlines()[0] if text else ""
        if pattern_title.search(title) or pattern_table.search(text):
            linked.append(name)
    return tuple(linked)


def build_guide_index() -> str:
    """One line per guide: name, title, section headings."""
    lines = []
    for name, path in _guide_files().items():
        title, sections = _title_and_sections(path.read_text())
        lines.append(f"- {name}: {title or '(untitled)'} | sections: {'; '.join(sections)}")
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# read_guide
# -----------------------------------------------------------------------------


def _resolve(name: str, files: dict[str, Path]) -> str | None:
    key = Path(name.strip()).stem.lower()
    for stem in files:
        if stem.lower() == key:
            return stem
    return None


def read_guide(names: list[str]) -> str:
    """Return the full markdown of one or more guides."""
    if isinstance(names, str):  # tolerate a bare string from the model
        names = [names]
    files = _guide_files()
    out = []
    for name in names[:_MAX_GUIDES_PER_READ]:
        stem = _resolve(name, files)
        if stem is None:
            out.append(f"## {name}\nError: no such guide. Available: {', '.join(files)}")
            continue
        out.append(f"===== guide: {stem} =====\n{files[stem].read_text().strip()}")
    if len(names) > _MAX_GUIDES_PER_READ:
        out.append(f"(Only the first {_MAX_GUIDES_PER_READ} guides were returned.)")
    return "\n\n".join(out)


def _prompt_context() -> str | None:
    index = build_guide_index()
    if not index:
        return None
    return (
        "## Business-rule guides\n"
        "The customer maintains markdown guides with business rules and metric "
        "definitions. These are authoritative: when a guide defines a term (e.g. what "
        "counts as 'completed', 'active', 'performing', 'legacy'), use the guide's "
        "definition instead of a conventional one.\n"
        "- list_schemas marks which guide is linked to each schema, and describe_table "
        "automatically attaches the full text of the guide(s) linked to the schema you "
        "describe. If no guide is attached, look for one in the index below (read_guide) "
        "or with search_guides.\n"
        # v3b: schema disambiguation (v2/v3a: Credit vs financial vs ccs/cs mix-ups).
        "- Several schemas can look alike. Prefer the schema that has a linked guide "
        "covering the question's domain, and check that its columns match the vocabulary "
        "the guide uses.\n"
        "- Apply only the rules that bear on the terms, filters, and metrics in the "
        "question. Guides also contain rules unrelated to the question; do not apply those.\n"
        "- Pay special attention to sections that give precise definitions or the scope "
        "of the data (e.g. 'definitions', 'scope', 'aggregation').\n"
        # v3b: question wins verbatim (v2 easy #40 added a guide qualifier to a term the
        # question had already defined).
        "- When the question itself defines a term or filter, use the question's "
        "definition exactly and do not add guide qualifiers to it. Use guide definitions "
        "for terms the question uses without defining.\n"
        # v3b: rule checklist (v2/v3a: guide read but applied only partly, e.g. missing
        # status = 'A' for "performing").
        "- Before calling submit_answer, list in your reasoning every term in the question "
        "that the guide (or question) defines, and the SQL clause that implements each "
        "one (filter, join, CASE, grouping key, denominator). Add any that are missing.\n\n"
        "Guide index (name: title | sections):\n" + index
    )


READ_GUIDE: Tool = Tool(
    name="read_guide",
    description=(
        "Read the full text of one or more business-rule guides by name (see the guide "
        "index in the system prompt). Read the relevant guide before writing SQL."
    ),
    parameters={
        "type": "object",
        "properties": {
            "names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Guide names from the index, e.g. ['credit_card_operations'].",
            },
        },
        "required": ["names"],
    },
    function=read_guide,
    prompt_context=_prompt_context(),
)


# -----------------------------------------------------------------------------
# search_guides
# -----------------------------------------------------------------------------


def search_guides(query: str) -> str:
    """Case-insensitive keyword search over all guides; returns matching lines."""
    terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 2]
    if not terms:
        return "Error: provide one or more keywords (3+ characters)."
    phrase = " ".join(re.findall(r"\w+", query.lower()))
    texts = {name: path.read_text() for name, path in _guide_files().items()}
    # v3a: IDF weighting so common words ("time", "data") don't dominate, plus a bonus
    # for the whole phrase. With full guides returned, ranking quality matters more.
    n_docs = max(len(texts), 1)

    def tokens(s: str) -> set[str]:
        # Whole-word matching with naive plural folding ("wins" -> "win"), so that
        # "win" no longer matches "wine" as a substring would.
        out = set()
        for tok in re.findall(r"\w+", s.lower()):
            out.add(tok)
            for suffix in ("es", "s"):
                if tok.endswith(suffix) and len(tok) > len(suffix) + 2:
                    out.add(tok[: -len(suffix)])
        return out

    doc_tokens = {name: tokens(txt) for name, txt in texts.items()}
    idf = {
        t: math.log((n_docs + 1) / (1 + sum(t in toks for toks in doc_tokens.values())))
        for t in terms
    }
    hits: list[tuple[float, str, str, str]] = []
    for name, text in texts.items():
        section = ""
        for line in text.splitlines():
            if line.startswith("#"):
                section = line.lstrip("#").strip()
                continue
            line_tokens = tokens(line)
            score = sum(idf[t] for t in set(terms) if t in line_tokens)
            if len(terms) > 1 and phrase in " ".join(re.findall(r"\w+", line.lower())):
                score *= 2
            if score > 0:
                hits.append((score, name, section, line.strip()))
    if not hits:
        return f"No guide lines match {terms}."
    hits.sort(key=lambda h: -h[0])
    shown = hits[:_MAX_SEARCH_HITS]
    lines = [f"[{name} / {section}] {text}" for _, name, section, text in shown]
    more = f"\n({len(hits) - len(shown)} more matches not shown)" if len(hits) > len(shown) else ""

    # v3a: also return the FULL text of the best-matching guides. In v2, 3 hard failures
    # used only the search fragments and never read the full guide, so they missed
    # definitions that sat next to the matched line (a nudge in the output was not
    # enough). Guides are short (~1k tokens), so returning the top ones whole removes
    # this failure mode structurally.
    # Rank guides by their best line, with a small bonus for breadth, so a guide that
    # mentions a common word many times does not outrank one with the exact phrase.
    best: dict[str, float] = {}
    total: dict[str, float] = {}
    for score, name, _, _ in hits:
        best[name] = max(best.get(name, 0.0), score)
        total[name] = total.get(name, 0.0) + score
    guide_score = {n: best[n] + 0.1 * total[n] for n in best}
    top = sorted(guide_score, key=lambda n: -guide_score[n])[:_FULL_GUIDES_IN_SEARCH]
    return (
        f"{len(hits)} matching lines (best first):\n" + "\n".join(lines) + more
        + "\n\nFull text of the best-matching guide(s). Apply the definitions relevant to "
        "the question, not just the matched lines:\n\n" + read_guide(top)
    )


SEARCH_GUIDES: Tool = Tool(
    name="search_guides",
    description=(
        "Keyword search across all business-rule guides. Returns matching lines with "
        "their guide and section, plus the full text of the best-matching guide(s)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keywords, e.g. 'legacy workforce'."},
        },
        "required": ["query"],
    },
    function=search_guides,
)


GUIDE_TOOLS: list[Tool] = [READ_GUIDE, SEARCH_GUIDES]
