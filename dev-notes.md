# Dev Notes

Working log for the Hecks takehome: findings, iteration plan, and eval results.
The final write-up will be distilled from this file.

Each iteration follows the same loop:
**Current result → Findings → Proposed changes → Result after change**

## Goal

- Pass ~all of `evals_easy` and 80–90% of `evals_hard` with the default model
  `openai/gpt-oss-120b:nitro`, without overfitting to this warehouse or these guides.
- Each iteration = one commit with a concise title and a detailed body (what changed, why).

## Constraints

- `evaluation/evaluate.py`: may only modify `create_tools()`.
- Eval workers build `OpenRouterConfig(api_key=...)` with defaults, so any model/LLM
  tuning must be done via the dataclass defaults in `framework/llm.py`.
- Guide files must not be edited.
- No hand-maintained schema map; structure must be discovered from the DB and docs.
- Model is fixed to the default `openai/gpt-oss-120b:nitro`.
- `first_token_timeout=10s` is kept as-is until hard pass rate reaches ~70%. Then we will
  check whether it costs pass rate.

## Commands

```bash
# load key from .env (variable name: KEY; never echo it)
set -a; source .env; set +a

uv run evaluate --api-key "$KEY" --split both --concurrency 16
uv run evaluate --api-key "$KEY" --split easy --concurrency 16
uv run evaluate --api-key "$KEY" --split hard --concurrency 16
# add -v for per-case failure details (gold vs submitted dataframes)
# traces: logs/run_<UTC timestamp>/evals_{easy,hard}/<uuid>.json
```

## Results summary

| Ver | Commit | Easy | Hard | Tokens (in/out, per split) | Logs |
|-----|--------|------|------|----------------------------|------|
| v0 baseline | d76b1c9 | 7/64 (10.9%) | 0/64 (0.0%) | easy 20k/27k, hard 23k/25k | `run_20261125_061236`, `run_20261125_061608` |
| v0 + error handling | TBD | TBD | TBD | TBD | |

---

## Background: initial codebase review

### Agent
- The agent has a single tool, `submit_answer`. It cannot see schemas, run SQL, or read
  guides, so it is writing SQL blind.
- System prompt (`framework/agent.py::_get_system_message`) only says "always call
  submit_answer".
- `framework/database.py` already has `list_schemas`, `list_tables`, `describe_table`
  helpers that are not exposed as tools.
- LLM defaults: `gpt-oss-120b:nitro`, temperature 0.6, no reasoning config,
  `first_token_timeout=10s`, `max_iterations=30`.

### Eval data
- `evals_easy.json` and `evals_hard.json` contain the **same 64 questions**. The easy
  version appends the relevant business rules to the prompt; the hard version omits them.
  - Easy measures schema discovery and SQL skill.
  - The easy-to-hard gap measures the ability to find and apply the right guide rules.
- Gold queries touch only ~12 schemas (ErgastF1, financial, lahman_2014, Credit,
  employee, CraftBeer, world, Chess, Airline, Hockey, imdb_MovieLens, Accidents).

### Database
- 76 schemas, 5,172 columns. That is far too large to put in a prompt, so the agent must
  locate the right schema itself.

### Guides
- 49 markdown files, ~170 KB total (~45k tokens), individually 1–4.5 KB.
- Many guides are distractors for databases that do not exist (AquariumDB, VolcanoDB,
  CryptoExchange, ...).
- The title line usually names the database, e.g. `(Credit Database)`, but not always
  (`Financial Database Business Rules`, `Hockey Statistics Guidelines`).
- Guides also contain irrelevant or distractor rules. The decisive definitions usually
  sit in trailing sections such as "Aggregation definitions" and "Scope and available
  data". The agent should read the full relevant guide and apply only the rules that
  apply to the question.

### Scoring (README / `evaluation/compare.py`)
- Column names and order are ignored, and extra columns are allowed.
- Row count must match exactly, including duplicates.
- Numbers are compared with abs tolerance 1e-4. Numeric strings do not match numbers.
- Implication: do not round unless asked, do not add or drop rows (LIMIT, DISTINCT), and
  respect the stated tie-breaks for top-N.

---

## v0 — Baseline + provider error handling

### Current result
- Easy: **7/64 (10.9%)**. Hard: **0/64 (0.0%)**.

### Findings

Easy (`logs/run_20261125_061236`):

| Outcome | Count | Cause |
|---|---|---|
| PASS | 7 | All 7 are `world.*` questions. The model guessed `world.Country/City/CountryLanguage` from pretraining (the classic MySQL sample DB). |
| SQL_ERROR | 44 | Hallucinated table names (`results`, `flights`, `transactions`, `beers`, `loans`, `dept_emp`, ...) → `Catalog Error: Table ... does not exist`. |
| MISMATCH | 4 | The model tried to explore by *submitting* `SELECT ... FROM information_schema.tables`. `submit_answer` is its only tool, so exploration ends the episode. |
| EXCEPTION | 9 | `400 Bad Request` from OpenRouter on the very first call (0 iterations). |

Hard (`logs/run_20261125_061608`):

| Outcome | Count | Cause |
|---|---|---|
| PASS | 0 | Even the `world.*` questions failed. |
| SQL_ERROR | 49 | Hallucinated table names, same as easy. |
| MISMATCH | 15 | **All 15** are exploration queries (`information_schema`) submitted as the answer. |
| EXCEPTION | 0 | No 400s this run, which supports the "transient" hypothesis. |

Observations:
- 54/64 easy cases finished in exactly one iteration: the model submits immediately
  because it has nothing else to do.
- Without the rules in the prompt (hard), the model is less confident and tries to
  explore more often (4 → 15 exploration submits). It has no exploration tool, so
  every attempt to explore consumes its one answer.
- The bottleneck is entirely the lack of discovery tools, not SQL reasoning.
- The 400s were transient: they appeared in one run and not the other, and only on
  some of 64 identical-shaped first requests. `llm.py` only retried 429 and
  ReadTimeout, and `raise_for_status()` dropped the response body, so the cause was
  invisible. These failures are infra noise worth ~14% of the easy split in that run.

### Proposed changes (v0 fix, implemented)
- `framework/llm.py`:
  - Retry on `{400, 408, 429, 500, 502, 503, 504}` plus ReadTimeout. 401/402/403 stay
    non-retryable so a bad key fails fast.
  - Replace `raise_for_status()` with `_raise_for_status_with_body()`, which puts the
    response body (first 1000 chars) into the exception, so trace files show the real
    cause.
  - Log each retry to stderr via tenacity `before_sleep`.
- Trade-off: a genuinely malformed request (e.g. context overflow) will now retry up
  to 5 times (~15s of backoff) before failing. That is acceptable for an eval harness.
- Known limitation (pre-existing, not changed): a ReadTimeout *mid-stream* re-issues
  the request, and chunks already yielded would be duplicated. Status errors happen
  before any chunk is yielded, so the new retries are safe.

### Result after change
- TBD (rerun both splits). Expect EXCEPTION ≈ 0. Pass rate itself should barely move,
  because the agent is still blind.

---

## v1 — Schema discovery + SQL execution tools (proposed, not implemented)

### Current result
- See v0 result after change.

### Findings (motivating v1)
- 44–49/64 failures are hallucinated tables, and 4–15 are exploration submitted as an
  answer. Both disappear if the agent can see the schema and run queries before
  submitting.

### Proposed changes
New `tools/database_tools.py`, registered in `create_tools()` (evaluate.py) and in
`interactive.py`:

1. `list_schemas()`: all schemas, each with its table names, read dynamically from
   `information_schema`. Roughly 2–3k tokens. One call is enough to locate the relevant
   database.
2. `describe_table(schema, table)`: column names and types plus ~3 sample rows. Sample
   rows expose real encodings (e.g. `status = 'A'`, `charge_code = 'RF'`). Possibly
   accept several tables per call to save round-trips.
3. `run_sql(query)`: read-only execution. Returns the row count and a ~20-row preview,
   or the DuckDB error verbatim so the model can self-correct. The DB is already opened
   with `read_only=True`.

System prompt (small change): explore first, validate with `run_sql`, then submit, and
state that `submit_answer` ends the task (no exploration via submit). Fuller prompt
engineering is deferred to v3 so v1's effect can be isolated.

Expected: easy → 80%+. Hard rises too, but the missing-rules gap becomes visible.

### Result after change
- TBD

---

## Later iterations (sketch)

| Ver | Change | Target |
|-----|--------|--------|
| v2 | Guide tools: `list_guides` (auto-extracted titles + headings), `read_guide` (full text), maybe `search_guides` | Large hard gain |
| v3 | System prompt: explicit workflow (locate schema → read guide → explore → validate → submit); generic output conventions derived from the scoring rules; "apply only relevant rules" | Hard → 70%+ |
| v4 | Failure-driven fixes: standalone trace-analysis script (no changes to evaluate.py), pre-submit SQL validation, reasoning effort, max_iterations, timeout | Hard → 80–90% |

Noise: temperature 0.6 and 64 cases (1 case ≈ 1.6%). Run key versions ≥2 times and
report a range.

Anti-overfitting rules: never copy rule text from the easy prompts into the system
prompt; no schema-specific logic.
