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

# debug tool (after each eval run). No paths needed: everything defaults to the latest
# run that contains the split.
uv run python scripts/debug_eval.py summary --label vN    # diagnose every case + tag version
uv run python scripts/debug_eval.py case <#> [--split easy] [--thinking]
uv run python scripts/debug_eval.py diff                  # latest vs previous *version*
uv run python scripts/debug_eval.py history               # all runs, pass rates
# diff auto-pick: B = latest run, A = latest earlier run with a different label (per
# split). That is why --label matters. Explicit: diff RUN_B / diff RUN_A RUN_B.
# reports are also saved to RUN_DIR/debug/ (logs/ is gitignored = local history)

# checks (run before each commit)
uv run pytest -v                 # unit tests in tests/
uvx ruff check <changed files>   # lint (config in pyproject.toml [tool.ruff])
uvx ty check                     # type check (config in pyproject.toml [tool.ty])
```
Note: `ruff check .` reports one pre-existing E501 (line too long) in
`framework/stream_printer.py`. That file is untouched, so we lint only the files we
change.

## Results summary

| Ver | Commit | Easy | Hard | Tokens (in/out, per split) | Logs |
|-----|--------|------|------|----------------------------|------|
| v0 baseline | d76b1c9 | 7/64 (10.9%) | 0/64 (0.0%) | easy 20k/27k, hard 23k/25k | `run_20261125_061236`, `run_20261125_061608` |
| v0 + error handling | 892af65 | not run separately | — | — | Validated as part of v1 |
| v1 tools + tool-call stream fix | 0a0e8ca | 57/64 (89.1%) | 20/64 (31.2%) | easy 1.55M/113k, hard 1.64M/84k | `run_20261125_070338` |
| v2 guide index + read/search guides | TBD | 54/64 (84.4%) | 51/64 (79.7%) | easy 2.35M/103k, hard 2.68M/92k | `run_20261125_080612` |

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
- Not run separately; validated together with v1.

### Correction (found during v1)
The "transient 400" hypothesis above was **probably wrong**. The likely cause is a
deterministic tool-call streaming bug in `llm.py`; see v1 → "New findings". Evidence:
- The 400 body (now visible thanks to the v0 change) reads
  `tool messages must include a non-empty string tool_call_id`.
- "0 iterations" in the v0 traces was an artifact, not evidence of a first-call failure.
  `evaluate.py` discards the collected events when the agent raises, so every exception
  trace has 0 events. Their durations were 2–5 s, which fits a few turns before failing.
- In v0, a split `submit_answer` call would produce invalid JSON (not submitted) plus a
  phantom call with an empty id. The next request then fails with 400, matching the
  observed pattern.

Retrying 400 is therefore mostly wasted on these deterministic failures (~15 s of
backoff each). **Decision (v1): 400 removed from the retry set.** The retry set is now
`{408, 429, 500, 502, 503, 504}`, and a 400 fails fast with its body in the trace.

---

## v1 — Schema discovery + SQL execution tools (implemented, pending eval)

### Current result
- v0: easy 7/64, hard 0/64.

### Findings (motivating v1)
- 44–49/64 failures are hallucinated tables, and 4–15 are exploration submitted as an
  answer. Both disappear if the agent can see the schema and run queries before
  submitting.

### New findings (while implementing v1)

**1. Tool-call streaming bug (framework, `llm.py`).** The first end-to-end smoke test
crashed on turn 4 with
`400: tool messages must include a non-empty string tool_call_id`. Raw SSE deltas from
the provider (Nebius, routed via `:nitro`) show it sometimes emits the *tail* of a tool
call's arguments under a **new index with an empty id and name**:

```
{"index": 0, "id": "chatcmpl-tool-...", "function": {"name": "run_sql", "arguments": ""}}
{"index": 0, "function": {"arguments": "{\n  \"query\": \"SELECT DISTINCT type FROM financial.trans\"\n"}}
{"index": 1, "id": "", "function": {"name": "", "arguments": ""}}      <- spurious
{"index": 1, "function": {"arguments": "}"}}                           <- the closing brace
```

The accumulator treated index 1 as a second call, which caused two failures:
- `run_sql` got invalid JSON, so its arguments became `{}` and it errored.
- A phantom call with an empty `tool_call_id` was sent back, and the next request got a
  400. The run dies.

This gets worse with more tool calls per episode, so it would have capped v1 badly.

**2. Schema selection among look-alike schemas.** In a 2-case smoke test (post-fix),
both cases failed by picking the wrong but plausible schema:
- A credit-card question went to `ccs.transactions_1k`; the gold query uses
  `Credit.charge`.
- A bank-account question went to `cs.ACCOUNT_TRANSACTIONS`; the gold query uses
  `financial.trans`.

The warehouse has several near-duplicate domains. Guides name their database
(`(Credit Database)`), so v2's guide tools should also help with schema selection, not
only with business rules.

**3. Token cost.** Each smoke-test case used ~70k prompt tokens over ~6–14 turns,
because the full history is resent every turn. That is fine for now. Context
compression (already in the framework, disabled) is an option if it grows.

### Proposed changes (implemented)
New `tools/database_tools.py`, registered in `create_tools()` (evaluate.py) and in
`interactive.py`:

1. `list_schemas()`: all schemas, each with its table names, read dynamically from
   `information_schema`. Roughly 2–3k tokens. One call is enough to locate the relevant
   database.
2. `describe_table(tables=[...])`: accepts up to 10 `schema.table` names per call.
   Returns column names and types, row count, and 3 sample rows. Sample rows expose real
   encodings (e.g. `status = 'A'`, `charge_code = 'RF'`).
3. `run_sql(query)`: read-only execution. Returns the row count, column dtypes, and a
   20-row preview, or the DuckDB error verbatim (including its "Did you mean ...?"
   hints) so the model can self-correct.
- Rendering: compact pipe tables, all columns visible, cells truncated at 80 chars,
  BLOBs shown as `<binary>`. Empty or whitespace-only strings are shown as `''`. This
  matters because `Credit.charge.charge_code` is `''` for 1.6M rows and NULL for 1, and
  a blank cell would hide that difference.

`framework/llm.py` (stream fix, see finding 1):
- A new tool-call index with no id and no name is aliased to the previous call, so its
  argument fragments are appended there.
- Any call still lacking an id gets a synthetic `call_<uuid>`.
- Unit tests: `tests/test_llm_stream.py` covers the Nebius pattern, real parallel calls,
  and the missing id case.

System prompt (small change): explore first, validate with `run_sql`, then submit, and
state that `submit_answer` ends the task (no exploration via submit). The
`submit_answer` description says the same. Fuller prompt engineering is deferred to v3
so v1's effect can be isolated.

Expected: easy → 60–80%+. Hard rises too, but the missing-rules gap becomes visible.
Schema mis-selection (finding 2) may cap easy below the 80% target until v2.

### Result after change
- **Easy: 57/64 (89.1%)**, up from 7/64. **Hard: 20/64 (31.2%)**, up from 0/64.
  Logs: `logs/run_20261125_070338`.
- Behavior: ~6.5 turns per case on average (max 30). Per split, the agent made ~66
  `list_schemas`, ~75 `describe_table`, ~205 `run_sql`, and ~63 `submit_answer` calls.
  The model really explores now.
- Zero 400s or other infra exceptions. The stream fix holds.
- Cost: ~3.4M tokens for both splits (~25k prompt tokens per case-turn, because the full
  history is resent every turn).

### Failure analysis

Analysis script (scratchpad for now): per-case failure type, schemas used by the gold
query vs the submitted query, and turn and tool counts.

**Easy, 7 failures:**

| Category | # | Example |
|---|---|---|
| Wrong look-alike schema | 2 | `ccs` instead of `Credit` (avg transaction value); `cs` instead of `financial` (account volume). |
| Ignored an explicit instruction in the prompt | 2 | Airline on-time: dropped "departure and arrival times present" from "completed", and read "NULL means none" as `WeatherDelay IS NULL`, so every carrier came out at 100.00%. Day-of-week: same missing `DepTime/ArrTime IS NOT NULL`, plus it added `COALESCE`. |
| Invented logic | 1 | Statement net amount: subtracted `Credit.payment` (not asked) and ignored the $0.01 exclusion. |
| SQL pitfall: GROUP BY alias shadowed by a column | 1 | `SELECT COALESCE(sc.category,'Other') AS category ... GROUP BY category`. DuckDB binds `category` to the column `sc.category`, not the alias, so NULL and 'Other' become separate groups (7 rows vs 6). |
| Submitted untested SQL | 1 | `SQL_ERROR` from a GROUP BY binder error. The query was never run through `run_sql` before submitting. |

**Hard, 44 failures:**

| Category | # | Notes |
|---|---|---|
| MISMATCH with the correct schema | 41 | The agent found the right tables but did not know the business definitions (completed, performing, legacy, rookie, thin route, ...). This is the expected "missing rules" gap. |
| MISMATCH with the wrong schema | 1 | Avg transaction value went to `financial` instead of `Credit`. |
| AGENT_ERROR (max 30 iterations) | 2 | Looped on exploration without converging. Both are questions whose easy versions also struggled (CraftBeer IBU, financial "legacy"). |

Takeaways:
- **v1 met its goal.** Easy is at 89%. The remaining easy failures come from instruction
  adherence and validation, not from missing tools.
- **The hard gap is almost entirely missing business rules:** 41 of 44 failures used the
  correct schema. This strongly motivates v2.
- Smaller generic issues for v3/v4: literal adherence to stated definitions, result
  sanity checks (100% for every carrier should have looked suspicious), testing before
  submitting, GROUP BY alias shadowing, and runaway exploration loops.
- `[DEBUG] Empty response detected` appeared ~10 times across both splits. The existing
  continuation prompt handled it. Not a significant failure source.

---

## Tooling — `scripts/debug_eval.py` (between v1 and v2)

Why: the ad-hoc scratch analysis could list failures but not explain them. To debug a
case you had to diff gold and submitted SQL by hand. We want a repeatable local
debugger to run after every eval.

Design:
- Reads trace files only. It re-executes gold and submitted SQL (traces store no
  dataframes). It never touches `evaluate.py` or the agent.
- **Auto-diagnosis** per failed case: `WRONG_SCHEMA`, `ROWCOUNT`, `MISSING_COLUMN`,
  `VALUES_DIFFER` (with the offending gold columns), `ROW_ASSOCIATION`, `SQL_ERROR`,
  `MAX_ITERATIONS`, `NO_SUBMISSION`, `INFRA`. Column matching reuses the evaluator's own
  cell normalization and 1e-4 tolerance.
- **Missing-rules view:** easy and hard share questions and gold queries one-to-one by
  index (64/64), and 45/64 hard prompts are exact prefixes of the easy prompt. The extra
  easy text is the rule set the agent should have found in the guides. Debug-only; it is
  never shown to the agent.
- **Condition diff:** sqlglot extracts atomic WHERE/HAVING/JOIN-ON predicates from both
  queries and shows the ones only in gold (missed) and only in submitted (added).
- **Row diff:** rows present on only one side, with mismatched columns paired by
  position so the wrong values are visible.
- **Trajectory:** one line per tool call (args and truncated result), plus optional
  per-turn model reasoning (`--thinking`).
- **History:** every report is saved under `RUN_DIR/debug/`. The summary JSON doubles
  as a diagnosis cache. `history` builds a pass-rate table across all runs on the fly,
  and `--label` tags a run with its version.

Example (v1, hard #2): diagnosis `VALUES_DIFFER net_charge_volume`; condition diff
`+ charge_amt <> 0.01`; row diff `3684257.75` vs `3684257.78`. The agent missed the
"exclude $0.01 test transactions" rule, which is exactly the missing guide rule.

Example (v1, hard #0, `--thinking`): the model spends turns 3–5 guessing whether
"on-time" means `ArrDelay <= 0` or `<= 15`. It has no source of truth, which is direct
motivation for v2.

v1 hard diagnosis breakdown (from the tool): VALUES_DIFFER 25, ROWCOUNT 16,
MAX_ITERATIONS 2, WRONG_SCHEMA 1.

Known limitation: v0's "explore by submitting information_schema" cases show up as
`WRONG_SCHEMA`.

---

## v2 — Business-rule guides (implemented, pending eval)

### Current result
- v1: easy 57/64 (89.1%), hard 20/64 (31.2%).

### Findings (motivating v2)
- 41 of 44 hard failures used the correct schema but the wrong business definitions.
  The agent does not know the guides exist, and has no tool to read them.
- Look-alike schemas (Credit/ccs, financial/cs) caused 3 failures across the two
  splits. Guide titles name their database, e.g. `(Credit Database)`, so guides can also
  disambiguate schemas.
- Sizes: a guide index (file name + title + section headings) for all 49 guides is
  ~2.0k tokens. A single guide is ~400–1,200 tokens. Both are cheap compared with the
  ~25k-token turns we already pay for.

### Proposed changes
New `tools/guide_tools.py`:

1. **Guide index in the system prompt.** Built at startup by scanning the guides
   directory: file name, H1 title, and H2/H3 headings, all extracted automatically, with
   no hand-written mapping. Rationale: in v1 the model never looked for business rules
   because nothing told it they existed. A tool-only approach (`list_guides`) relies on
   the model deciding to call it. A 2k-token index makes the guides always visible.
2. `read_guide(name)`: returns the full markdown of one guide (or several). We do not
   chunk: guides are short, and the decisive definitions often sit in a trailing
   section ("Aggregation definitions", "Scope and available data") that a snippet
   retriever could miss.
3. `search_guides(query)` (optional): case-insensitive keyword search across all guides,
   returning file, section, and matching lines. It covers guides whose title does not
   name the schema (`Financial Database Business Rules`, `Hockey Statistics
   Guidelines`) and terms defined in an unexpected guide.
- The guides directory defaults to `evaluation/data/guides` and can be overridden with
  an env var, so the same code works on another customer's docs.

System prompt additions (kept short; the full prompt rework is v3):
- Before writing SQL, identify the relevant guide(s) from the index and read them in
  full.
- Business definitions in the guide override intuitive or conventional definitions
  (e.g. what counts as "completed", "performing", "legacy").
- Guides contain rules irrelevant to the question, so apply only those that bear on the
  terms and metrics asked about.
- If the question's own wording conflicts with a guide, follow the question.

Expected: hard 31% → 60–75%. Easy should hold at ~89% or rise slightly, because guides
also help with schema choice. Token cost rises modestly (+1–2 tool calls per case).

Risks:
- Over-application: the model applies distractor rules, e.g. excluding "reset months"
  or splitting into "Premium" tiers when not asked. Mitigated by the
  "only relevant rules" instruction, and we will watch for it in failure analysis.
- Index-in-prompt scales poorly to hundreds of guides. For this warehouse (49) it is
  fine. The write-up should note that a larger corpus would need retrieval.

**Decision:** put the index in the system prompt (rather than a `list_guides` tool).

### Implementation notes
- `framework/agent.py`: `Tool` gets an optional `prompt_context` field, and the system
  message appends every tool's context. `evaluate.py` builds `Agent(config, tools)` and
  we may only change `create_tools()`, so a tool is the only channel for adding standing
  context. This keeps the framework generic: it knows nothing about guides.
- `tools/guide_tools.py`: `read_guide(names=[...])` (up to 4, full text) carries the
  index plus guidance rules as its `prompt_context`. `search_guides(query)` returns up to
  40 matching lines with `[guide / section]`.
- System prompt is now ~2.4k tokens, of which ~2k is the index.
- Smoke test on 3 v1-failing hard cases: #2 (credit $0.01 rule) PASS, #8 (legacy
  workforce) PASS, #0 (airline on-time) FAIL. In #0 the model ran
  `search_guides("on-time")`, used the snippet, and never read the full airline guide,
  so it missed the "completed" and WeatherDelay rules in the same guide. Fix:
  `search_guides` output now ends with a nudge to read the full candidate guides before
  writing SQL.

### Result after change
- **Hard: 51/64 (79.7%)**, up from 20/64: +32 fixed, 1 regressed. **Easy: 54/64
  (84.4%)**, down from 57/64: +2 fixed, 5 regressed. Logs: `run_20261125_080612`.
- Cost: ~5.0M tokens for both splits, vs 3.2M in v1. The 2k-token index is resent on
  every turn (~6.5 turns/case).

### Failure analysis (via `scripts/debug_eval.py`)

**Hard, 13 failures, grouped by root cause:**

| Root cause | Cases | Detail |
|---|---|---|
| Searched but never read the full guide | #4, #53, #57 | `search_guides` only, so it missed rules from the same guide ($0.01 exclusion, "completed" = Dep/ArrTime present). The nudge in the search output was not enough. |
| Read the guide, applied it incompletely | #0, #5, #20, #29 | #0 read `airline_operations` and still missed "completed". #20 used `constructorResults` instead of individual results. #5 is a complex multi-step grouping rule. #29 hit the NULL pitfall below. |
| Used no guide at all | #46, #49, #63 | Chess, F1, and Hockey questions where the agent never searched or read a guide. |
| Wrong look-alike schema | #3, #6 | #3: `search_guides("transaction value")` led to `financial_operations`, so it used `financial.trans` instead of `Credit.charge`. #6: read the financial guide but still used `cs`. |
| Submitted untested SQL | #9 | Binder error; regressed from v1 PASS. |

Cross-cutting **SQL NULL pitfall**: `charge_code <> 'RF'` silently drops NULL codes,
although the rule says "NULL codes are purchases". The correct form is
`charge_code IS DISTINCT FROM 'RF'`. Seen in hard #29 and #57, and in easy #57.

**Easy regressions (5): mostly not caused by guides.**
- #15 (chess win rate): misread "draws count as games in the denominator" and used
  wins+draws as the denominator. Did not read a guide.
- #40 (thin routes): guide-induced. `search_guides("completed flight definition")` → the
  guide says "diverted flights count as completed", so the agent added
  `OR Diverted = 1` even though the question defines completed explicitly. This is the
  over-application risk we flagged.
- #48 (production breweries): grouped by brewery id where gold groups by name. A
  grouping-key ambiguity.
- #57 (long-tail providers): the NULL pitfall above.
- #59 (starting pitchers): different top-50 membership with the same stated filters.
  Ranking or tie-break detail. Did not read a guide.

Only 1 of the 5 regressions is clearly guide-induced. The others look like
instruction-reading or SQL errors that occur stochastically (temperature 0.6). With 64
cases, ±3 is plausibly noise. **We need repeated runs to tell signal from noise.**

Takeaways:
- v2 delivered the big structural gain (hard +31 cases). Remaining failures are about
  *how reliably* the agent uses the guides, plus a few generic SQL and validation issues.
- Easy ≈ hard now (84% vs 80%). The agent reaches roughly the same accuracy whether or
  not the rules are spelled out in the prompt.

---

## v3 — Reliable guide use + validation (proposed, not implemented)

### Current result
- v2: easy 54/64 (84.4%), hard 51/64 (79.7%).

### Proposed changes (ranked by expected impact / cost)

1. **Pre-submit validation in `submit_answer`** (fixes #9-type failures).
   `submit_answer` executes the query first. If it errors, the tool returns the error
   instead of `ANSWER_SUBMITTED:`, so the episode continues and the agent fixes it. The
   tool is stateless, so this is safe under concurrency. Low risk.
2. **Make reading the full guide a required step** (targets the 3 search-only and 3
   no-guide cases). Two options:
   - (a) Prompt-only: restructure the workflow as a numbered checklist with "read the
     full guide for the chosen schema, even if search found the term" as a mandatory
     step.
   - (b) Structural: `search_guides` returns the full text of the top-matching guide(s)
     instead of fragments, which removes the "snippet only" failure mode. Guides are
     small, so the cost is ~1k tokens per search.
   Recommendation: (b) plus a lighter version of (a). (b) is generic and does not depend
   on the model obeying.
3. **Rule checklist before submitting** (targets "read but applied incompletely":
   #0, #20, #29). Instruct the model to list, in its reasoning, every guide definition
   that applies to a term in the question and map each one to a SQL clause, then check
   the query against that list before calling submit_answer.
4. **Generic SQL pitfalls in the system prompt** (NULL pitfall #29, #57, easy #57;
   GROUP BY alias from v1):
   - Excluding a value from a nullable column: use `IS DISTINCT FROM`; `col <> 'X'`
     drops NULLs.
   - GROUP BY / ORDER BY an output alias that shadows an input column binds to the
     column: group by the full expression.
   - Do not round unless asked; keep the row grain the question asks for.
5. **Schema disambiguation rule** (#3, #6): "When several schemas look alike, use the
   database named in the relevant guide's title, and verify with describe_table that the
   columns match the guide's vocabulary."
6. **Question beats guide**, made more explicit (easy #40): when the question itself
   defines a term, use the question's definition verbatim and do not add guide
   qualifiers to it.

### Measurement plan
- Run v3 **twice** (both splits) and report the mean and range. Also rerun v2 once more
  if budget allows, so the v2→v3 delta is not just noise.
- Watch: easy regressions, guide over-application, token cost (option 2b adds tokens).

### Result after change
- TBD

---

## Later iterations (sketch)

| Ver | Change | Target |
|-----|--------|--------|
| v4 | Leftovers after v3: loop and iteration control (max-iteration failures); reasoning effort; `first_token_timeout` check (deferred until ~70%, now reached); context compression for token cost | Hard → 85–90% |

Noise: temperature 0.6 and 64 cases (1 case ≈ 1.6%). Run key versions ≥2 times and
report a range.

Anti-overfitting rules: never copy rule text from the easy prompts into the system
prompt; no schema-specific logic.
