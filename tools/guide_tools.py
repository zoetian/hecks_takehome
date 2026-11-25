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
_MAX_SEARCH_HITS = 40
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
        "- Before writing SQL, find the guide(s) for the schema/domain in the index below "
        "and read them in full with read_guide. Guide titles often name their database, "
        "which also helps pick the right schema among look-alikes.\n"
        "- Apply only the rules that bear on the terms, filters, and metrics in the "
        "question. Guides also contain rules unrelated to the question; do not apply those.\n"
        "- Pay special attention to sections that give precise definitions or the scope "
        "of the data (e.g. 'definitions', 'scope', 'aggregation').\n"
        "- If the question's own wording explicitly conflicts with a guide, follow the "
        "question.\n"
        "- Use search_guides to look up a term when you are unsure which guide defines it.\n\n"
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
    hits: list[tuple[int, str, str, str]] = []
    for name, path in _guide_files().items():
        section = ""
        for line in path.read_text().splitlines():
            if line.startswith("#"):
                section = line.lstrip("#").strip()
                continue
            low = line.lower()
            score = sum(1 for t in terms if t in low)
            if score:
                hits.append((score, name, section, line.strip()))
    if not hits:
        return f"No guide lines match {terms}."
    hits.sort(key=lambda h: -h[0])
    shown = hits[:_MAX_SEARCH_HITS]
    lines = [f"[{name} / {section}] {text}" for _, name, section, text in shown]
    more = f"\n({len(hits) - len(shown)} more matches not shown)" if len(hits) > len(shown) else ""
    guides = sorted({name for _, name, _, _ in shown})
    # Smoke test (hard #0): the model used a search snippet for "on-time" and skipped
    # the rest of the guide, missing the "completed flight" and weather rules next to it.
    nudge = (
        "\nThese are fragments only. Related definitions usually sit elsewhere in the same "
        f"guide: read the relevant guide(s) in full with read_guide (candidates: "
        f"{', '.join(guides)}) before writing SQL."
    )
    return f"{len(hits)} matching lines (best first):\n" + "\n".join(lines) + more + nudge


SEARCH_GUIDES: Tool = Tool(
    name="search_guides",
    description=(
        "Keyword search across all business-rule guides. Returns matching lines with "
        "their guide and section. Use it to find where a term is defined; then read that "
        "guide in full with read_guide."
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
