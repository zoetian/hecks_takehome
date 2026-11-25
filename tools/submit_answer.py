"""Tool for submitting a final SQL query answer to the evaluation pipeline."""

import hashlib

from framework.agent import ANSWER_SUBMITTED_PREFIX, Tool
from framework.database import execute_query
from tools.sql_lint import lint_null_comparisons


def _confirmation_code(query: str) -> str:
    """Short code derived from the query text, so confirmation stays stateless."""
    return hashlib.sha1(query.strip().encode()).hexdigest()[:8]


def submit_answer(query: str, confirmation_code: str = "") -> str:
    """Submit a SQL query as the final answer to the evaluation.

    This tool signals that the agent has completed its reasoning and is
    ready to submit a final answer.

    Args:
        query: The SQL query to submit as the final answer.

    Returns:
        A confirmation message indicating that the submission has been received.
    """
    # v3a: validate before accepting. In v1 and v2, some cases failed because the agent
    # submitted SQL it had never run (binder errors). If the query fails, return the
    # error WITHOUT the submitted prefix, so the episode continues and the agent can fix
    # it. This function is stateless, so it is safe for concurrent eval workers.
    result = execute_query(query)
    if not result.is_success:
        return (
            "Submission rejected: the query failed to execute, so it was NOT submitted. "
            f"Fix it (test with run_sql) and call submit_answer again.\n{result.error_message}"
        )

    # v5: warn about queries that run but are likely silently wrong, and require an
    # explicit confirmation. In v4 run 2, #13 submitted an untested 0-row result and #56
    # used `charge_code <> 'RF'`, which drops NULL codes. We don't hard-block, since
    # either can be intended. The agent resubmits with the confirmation code from the
    # warning. A plain boolean was tried first, but the model set confirmed=true
    # pre-emptively on its first submit (2/4 smoke runs), bypassing the check. A code
    # derived from the query can only be obtained by receiving the warning, and it
    # stays stateless. Checked offline: 0/64 gold queries trigger a warning.
    if confirmation_code.strip() != _confirmation_code(query):
        warnings = lint_null_comparisons(query)
        df = result.dataframe
        if df is not None and df.height == 0:
            warnings.append(
                "The query returns 0 rows. Check filter values against the actual data "
                "(e.g. SELECT DISTINCT on filtered columns: case, spelling, codes) before "
                "concluding the answer is empty."
            )
        if warnings:
            return (
                "Submission NOT accepted yet. Review these possible issues:\n- "
                + "\n- ".join(warnings)
                + "\nFix the query if needed and submit again. If the query is already "
                "correct as written, call submit_answer again with the same query and "
                f"confirmation_code='{_confirmation_code(query)}'."
            )

    # Return the prefix followed by the query. The prefix signals the agent to stop.
    # IMPORTANT: Do not add any text after the query - the extraction logic in the evals
    # takes everything after the prefix as the query.
    return f"{ANSWER_SUBMITTED_PREFIX}{query}"


SUBMIT_ANSWER: Tool = Tool(
    name="submit_answer",
    description=(
        "Submit your final SQL query as the answer to the current question. "
        "Use this tool when you are confident that your query correctly "
        "answers the question. This ends the task: do not use it to explore "
        "(use run_sql for that). The query is executed first; if it errors, the "
        "submission is rejected and you must fix it. If it returns 0 rows or has a "
        "NULL-dropping comparison, you get warnings and must fix the query or resubmit "
        "with the confirmation code given in the warning."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "The final SQL query to submit as your answer. "
                    "Must be valid SQL syntax with table names qualified "
                    "by schema names (e.g., 'SELECT * FROM schema.table')."
                ),
            },
            "confirmation_code": {
                "type": "string",
                "description": (
                    "Leave empty. Only when submit_answer returned warnings and you checked "
                    "that the query is correct as written, resubmit with the code it gave."
                ),
            },
        },
        "required": ["query"],
    },
    function=submit_answer,
)
