"""Tests for agent tools that need no LLM (require hecks.duckdb to be present)."""

import pytest

from framework.agent import ANSWER_SUBMITTED_PREFIX
from framework.database import DATABASE_PATH
from tools.guide_tools import search_guides
from tools.submit_answer import submit_answer

needs_db = pytest.mark.skipif(not DATABASE_PATH.exists(), reason="hecks.duckdb not present")


@needs_db
def test_submit_answer_accepts_valid_query() -> None:
    assert submit_answer("SELECT 1 AS x") == f"{ANSWER_SUBMITTED_PREFIX}SELECT 1 AS x"


@needs_db
def test_submit_answer_rejects_failing_query() -> None:
    out = submit_answer("SELECT * FROM no_such_schema.no_such_table")
    assert not out.startswith(ANSWER_SUBMITTED_PREFIX)
    assert "rejected" in out


def test_search_guides_includes_full_guide_text() -> None:
    out = search_guides("legacy workforce")
    assert "===== guide:" in out  # full text appended, not only matching lines
