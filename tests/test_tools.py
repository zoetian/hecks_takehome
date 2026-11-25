"""Tests for agent tools that need no LLM (require hecks.duckdb to be present)."""

import re

import pytest

from framework.agent import ANSWER_SUBMITTED_PREFIX
from framework.database import DATABASE_PATH
from tools.guide_tools import search_guides
from tools.submit_answer import submit_answer


def _code_from(warning: str) -> str:
    match = re.search(r"confirmation_code='(\w+)'", warning)
    assert match is not None
    return match.group(1)


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


def test_guides_for_schema_links_by_title_or_qualified_table() -> None:
    from tools.guide_tools import guides_for_schema

    assert guides_for_schema("Credit") == ("credit_card_operations",)
    assert guides_for_schema("financial") == ("financial_operations",)  # title "Financial ..."
    assert guides_for_schema("no_such_schema") == ()


@needs_db
def test_describe_table_attaches_linked_guide_and_list_schemas_tags_it() -> None:
    from tools.database_tools import describe_table, list_schemas

    out = describe_table(["Credit.charge"])
    assert "===== guide: credit_card_operations =====" in out
    assert "[guide: credit_card_operations]" in list_schemas()


@needs_db
def test_submit_answer_warns_on_null_dropping_comparison_until_confirmed() -> None:
    q = "SELECT COUNT(*) FROM Credit.charge WHERE charge_code <> 'RF'"
    out = submit_answer(q)
    assert not out.startswith(ANSWER_SUBMITTED_PREFIX) and "IS DISTINCT FROM" in out
    assert submit_answer(q, confirmation_code="wrong").startswith("Submission NOT accepted")
    code = _code_from(out)
    assert submit_answer(q, confirmation_code=code) == f"{ANSWER_SUBMITTED_PREFIX}{q}"
    ok = "SELECT COUNT(*) FROM Credit.charge WHERE charge_code IS DISTINCT FROM 'RF'"
    assert submit_answer(ok).startswith(ANSWER_SUBMITTED_PREFIX)


@needs_db
def test_submit_answer_warns_on_empty_result_until_confirmed() -> None:
    q = "SELECT * FROM Credit.member WHERE lastname = 'zzz-no-such-name'"
    out = submit_answer(q)
    assert "0 rows" in out
    code = _code_from(out)
    assert submit_answer(q, confirmation_code=code).startswith(ANSWER_SUBMITTED_PREFIX)


@needs_db
def test_null_lint_ignores_columns_without_nulls() -> None:
    from tools.sql_lint import lint_null_comparisons

    # member.lastname has no NULLs, so <> is safe there.
    assert lint_null_comparisons("SELECT 1 FROM Credit.member m WHERE m.lastname <> 'X'") == []
