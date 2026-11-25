"""Tool for submitting a final SQL query answer to the evaluation pipeline."""

from framework.agent import ANSWER_SUBMITTED_PREFIX, Tool
from framework.database import execute_query


def submit_answer(query: str) -> str:
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
        "submission is rejected and you must fix it."
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
        },
        "required": ["query"],
    },
    function=submit_answer,
)
