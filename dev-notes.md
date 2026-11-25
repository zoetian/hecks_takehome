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
| v2 guide index + read/search guides | 616d773 | 54/64 (84.4%) | 51/64 (79.7%) | easy 2.35M/103k, hard 2.68M/92k | `run_20261125_080612` |
| infra: max_tokens 16k, in-flight 402 retry | 35b99ca | — | — | — | (3 invalid 402 runs deleted) |
| v3a submit validation, full guides in search, SQL pitfalls | 0b7a913 | 55/64 (85.9%) | 52/64 (81.2%) | easy 2.23M/100k, hard 2.83M/100k | `run_20261125_084027` |
| v3b schema-linked guides in describe_table + prompt rules | 574a340 | not run | 55/64 (85.9%) | hard 2.88M/102k | `run_20261125_213950` |
| v4 temperature 0.6 → 0.2 (2 runs) | 6ac44f7 | not run | 58/64, 52/64 (mean 85.9%) | hard 2.53M/98k, 2.44M/93k | `run_20261125_215541`, `_215659` |
| v5 pre-submit NULL/empty lint with confirmation code (2 runs) | a521949 | not run | 54/64, 52/64 (mean 82.8%) | hard 2.56M/92k, 2.99M/104k | `run_20261125_221127`, `_221513` |
| **final** (v5 code, both splits) | a521949 | **60/64 (93.8%)** | **56/64 (87.5%)** | easy 2.21M/100k, hard 2.82M/97k | `run_20261125_221943` |

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

## v3 — Reliable guide use + validation

Split into two steps so each kind of change can be measured on its own:
- **v3a** = structural/tooling (①②④), implemented.
- **v3b** = prompt-only behavior (③⑤⑥), not yet.

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
- Token numbers here are OpenRouter (agent) tokens, i.e. what `uv run evaluate` prints,
  billed to the eval key. Claude Code's own usage while developing is separate.

### v3a implementation notes (①②④)
- ① `tools/submit_answer.py`: executes the query before accepting it. On error it
  returns "Submission rejected ..." plus the DuckDB error, without the
  `ANSWER_SUBMITTED:` prefix, so the episode continues and the agent can fix the query.
  Stateless, so it is safe under concurrency. Side fix in `framework/database.py`:
  `execute_query` raised `UnboundLocalError` from `finally` if `connect()` failed.
- ② `tools/guide_tools.py::search_guides`: results now include the full text of the
  top-2 guides (~2k tokens), not only the matching lines (cut to 15). Ranking was
  improved at the same time, since a wrong top guide now costs more:
  - IDF term weights, so common words like "time" do not dominate.
  - Whole-phrase bonus.
  - Whole-word matching with plural folding. Previously "win" substring-matched
    "wine".
  - Guides are ranked by their best-matching line plus 0.1 × total, so a guide that
    repeats a common word does not beat one with the exact phrase. On 17 typical
    queries, the relevant guide is in the top 2 for all 17.
- ④ `framework/agent.py` system prompt adds generic SQL pitfalls: NULL with
  `<>`/`NOT IN` (use `IS DISTINCT FROM`); output alias shadowing an input column in
  GROUP BY; no rounding unless asked and keep the row grain; sanity-check implausible
  results.
- Tests: `tests/test_tools.py` (submit accepts and rejects; search includes full guide
  text). 12 tests pass.
- Smoke test on 4 v2 hard failures (#9 untested SQL; #4, #53, #57 search-only): 4/4
  PASS (n=1 each). #9 passed on its first submit, so the rejection path was not
  exercised in that test.

### Result after change
- **v3a (1 run, `run_20261125_084027`): easy 55/64 (85.9%), hard 52/64 (81.2%)**, vs v2
  54/64 and 51/64. 0 infra failures: two in-flight 402s were retried successfully by
  the infra fix. Tokens 5.06M in / 0.20M out, about the same as v2. Only one run, to
  save budget.
- The targeted mechanisms worked on their target cases:
  - hard #9 (untested SQL) now passes, and 1 submission across the run was rejected and
    then fixed.
  - #4 and #57 (search-only) pass.
  - #29 and #57 (NULL pitfall) pass; 20 submitted queries now use `IS DISTINCT FROM`.
  - #6 (wrong schema) passes on both splits.
- **But the net change is only +1 per split, because of high churn:** hard had 8 fixed
  and 7 regressed; easy had 3 fixed and 2 regressed.
- Hard regressions: 3 of 7 (#21, #22, #24) never consulted any guide in this run, even
  though they passed in v2 when they did. #7, #41, #43, #55 read or searched a guide but
  applied it partly (e.g. #7 missed `status = 'A'` for "performing"). None is clearly
  caused by a v3a mechanism. They look like run-to-run variance in *whether and how* the
  agent uses guides.
- v3b: see below.

### v3b — structural guide exposure + prompt rules (implemented, evaluated on hard)

Redesigned after the v3a result. The originally planned prompt-only items (③⑤⑥) would
not address the main v3a failure mode: the agent *sometimes* not consulting a guide at
all. v3b makes guide exposure structural, and adds the prompt items as short rules.

- `tools/guide_tools.py::guides_for_schema(schema)`: a guide is linked to a schema if
  its title names the schema (case-insensitive word match, e.g. "(Credit Database)",
  "Financial Database ...") or its body references a schema-qualified table
  (`Credit.charge`). Inferred from the guides, with no hand-written map.
  - Validated: all 12 eval schemas link to exactly their guide, with no false links.
  - Plain word matching was rejected: "Credit", "world", and "financial" matched 6–8
    unrelated guides.
- `list_schemas` tags each schema with its linked guide (`[guide: ...]`), about +200
  tokens. This helps choose among look-alike schemas (Credit vs ccs, financial vs cs).
- `describe_table` appends the full text of the guide(s) linked to the described
  schemas (~650 tokens for the credit guide). The agent calls `describe_table` in
  nearly every episode right after choosing a schema, so it now always sees the rules.
  Trade-off: if `describe_table` is called several times, the guide is attached each
  time (tools are stateless).
- Prompt (guide `prompt_context`):
  - ⑤ prefer the schema whose linked guide covers the domain;
  - ⑥ when the question defines a term, use it verbatim and add no guide qualifiers;
  - ③ before submitting, list every defined term and the SQL clause that implements it.
- Tests: `guides_for_schema` linking, `describe_table` attachment, `list_schemas` tag
  (15 tests pass).
- Smoke test on 4 v3a hard regressions (#21, #22, #24 never consulted a guide; #7 applied
  it partly): **4/4 PASS** (n=1 each). None of the 4 called `read_guide` or
  `search_guides`; the rules arrived via `describe_table`.

#### v3b result (hard only, to save budget)
- **Hard: 55/64 (85.9%)**, up from 52/64 in v3a: 7 fixed, 4 regressed.
  Run `run_20261125_213950`. Tokens 2.88M in / 0.10M out, about the same as v3a
  (2.83M), so the attached guides did not raise cost noticeably.
- **Guide exposure is now structural:** 63 of 64 traces contain an auto-attached
  guide. All 3 "never consulted a guide" regressions from v3a (#21, #22, #24) pass.
- Remaining 9 failures. In every one of them the rules were in the context:

| Case | Diagnosis | What went wrong |
|---|---|---|
| #0, #53 (airline) | VALUES_DIFFER | Guide attached, but the agent still omits "completed = DepTime and ArrTime present". It filters only `Cancelled = 0`, or uses `ArrDelayMinutes IS NOT NULL`. |
| #3 (avg transaction value) | WRONG_SCHEMA | The question names no domain. The agent picks `financial.trans`; gold uses `Credit.charge`. Genuinely ambiguous wording. |
| #6 (account volume) | WRONG_SCHEMA | Picked `cs` (no linked guide) over `financial` (tagged). The schema tag did not override the look-alike table names. |
| #7 (Eastern Region loans) | VALUES_DIFFER | Missed `status = 'A'` ("performing") despite the attached guide and the checklist rule. |
| #27 (accidents) | VALUES_DIFFER | Joined the lookup table and lost rows, against the guide's "do not join" note. |
| #36 (F1 starts) | VALUES_DIFFER | Used `qualifying` instead of `results` + `status` exclusions. |
| #41 (statement net) | VALUES_DIFFER | Did not restrict to `Credit.statement` numbers. |
| #59 (starting pitchers) | VALUES_DIFFER | Top-50 membership differs. Its filter variant differs subtly from gold. |

#### Stability across v2, v3a, v3b (hard, 3 runs)
- **Always pass: 41/64.** Always fail: **3** (#0, #3, #53). Fail in 2 of 3: 5 (#6, #7,
  #20, #41, #49). **Fail in 1 of 3 ("flaky"): 15.**
- So ~20 cases are flaky and only 3 are hard failures. The ceiling with the current
  design is high, and the main lever left is consistency.
- Caveat: these are different versions, not repeated runs of one version. Some flips
  are real effects (e.g. #21/#22/#24 fixed by v3b), but most of the 1-of-3 group looks
  like noise.

### Key finding: run-to-run variance now dominates
- Between v2 and v3a, 15 of 64 hard cases flipped (8 fixed, 7 regressed) for a net of +1.
  At temperature 0.6 a single run cannot resolve improvements of a few cases.
- The largest remaining failure mode is **inconsistent guide consultation**: the same
  question sometimes reads the right guide and sometimes reads none. Prompt rules alone
  ("read the guide first") are followed only stochastically.
- Implications:
  1. The next change should make guide exposure *structural*, not optional.
  2. Lowering temperature is a cheap variance reducer worth testing.
  3. Single-run deltas of ±3 should not be reported as real effects.

### Incident: 3 invalid v3a runs (HTTP 402)
- Runs `run_20261125_082127`, `_082255`, `_082424` are **invalid**: ~120/128 cases per run
  failed with
  `HTTP 402 in_flight_budget_exhausted` ("This request would exceed your available
  credits given your current in-flight requests", Retry-After 120 s). They were later
  deleted from `logs/`, along with a 4th partial run (`_082949`) that was stopped after
  the infra fix still hit 402s (the account's credit balance was low; the user topped
  up).
- Note: "No limit set" on a key is a *spending limit*. The in-flight budget is bounded
  by the account's *credit balance* (plus a platform ceiling), so a key with no limit can
  still be capped.
- Mechanism: OpenRouter reserves credit for each in-flight request at its *maximum*
  possible cost. We send `max_tokens=100000` with 16 concurrent workers, i.e. we reserve
  room for ~1.6M output tokens at once.
- Actual need (v2 traces, 814 generations): median 169, p99 945, **max 1,563**
  completion tokens per generation. The reservation is ~64× larger than anything we use.
- Candidate fixes (infra, not agent behavior):
  1. Lower the `max_tokens` default to 16,384 (10× the observed max).
  2. Optionally retry this specific 402 (`in_flight_budget_exhausted`) using its
     Retry-After. A plain 402 (truly out of credits) should still fail fast.
  3. Lower concurrency if the balance is low.
- Also worth checking: the remaining credit balance on the key.
- Two different keys failed the same way: the first run used the user's own key, the
  other two the provided key. All three show only `in_flight_budget_exhausted`
  (121/124/123 of 128 cases). So the request pattern (`max_tokens=100000` × 16
  concurrent), not a single key's balance, is the common factor. The 4–7 cases per run
  that completed were presumably requests that happened to get budget while others
  settled.

---

## v4 — Lower sampling temperature (implemented, evaluated: 2 hard runs)

### Current result
- v3b: hard 55/64 (85.9%).

### Findings (motivating v4)
- Stability across v2/v3a/v3b: 41 always pass, 3 always fail, ~20 flip. Most remaining
  error is sampling variance, not missing capability.
- In v3b every failure had the rules in context. The model sometimes applies them and
  sometimes not.

### Change
- `framework/llm.py`: `temperature` default 0.6 → **0.2**. This is the only change, so
  its effect is isolated.
  - Not 0.0: greedy decoding can make reasoning models loop or repeat.
  - The model, tools, and prompt are unchanged.

### Hypothesis / what to look for
- Fewer flips. The "flaky" cases should settle, ideally on PASS where the agent's most
  likely behavior is correct.
- Risk: lower temperature also freezes *wrong* habits. A case whose most likely path is
  wrong (e.g. #0/#53 dropping the "completed" rule) becomes consistently wrong, and
  "lucky" passes disappear. Net effect is an empirical question.
- Proper measurement needs ≥2 runs at the same version (flip rate within a version).
  One run vs v3b mixes version effects with noise.

### Result after change (2 hard runs, same code)
- **Run 1 (`run_20261125_215541`): 58/64 (90.6%). Run 2 (`run_20261125_215659`): 52/64
  (81.2%). Mean 55.0/64 (85.9%)**, identical to v3b's single run. Zero infra errors in
  either run. The two runs were started ~78 s apart and ran concurrently (32 workers in
  total). In-flight 402s, if any, were retried.
- **Within-version flips: 10/64** (8 pass only in run 1, 2 pass only in run 2). This is
  our first direct measurement of run-to-run noise at a fixed version. Even at
  temperature 0.2 a single hard run has a spread of roughly ±3–5 cases.
- Run 2 failed 4 cases that passed in all 4 earlier runs (#13, #48, #50, #56):
  - #13 submitted untested SQL that returned **0 rows**. Pre-submit validation only
    rejects errors, not empty results.
  - #56 used `charge_code <> 'RF'` again (NULL pitfall) despite the prompt rule.
  - #48 grouped by brewery id instead of name (ambiguous grain).
  - #50 used a subtly different join.
- Consistent failures across all 5 runs (v2, v3a, v3b, v4 ×2): **#0, #3**. #7 fails in
  4 of 5.

### Interpretation
- No measurable gain from temperature 0.2 at this sample size (mean 55.0 vs 55). The
  10-flip spread shows that variance is not only sampling temperature. Plausible
  additional sources (not verified):
  - `:nitro` routes to whichever provider is fastest at that moment, and providers can
    differ numerically.
  - Long multi-turn trajectories amplify small early differences.
- We keep 0.2. It is no worse on the mean, theory favors it for repeatability, and the
  report should state "no measurable effect" honestly.
- Reporting implication: compare versions on means over ≥2 runs. **Single-run deltas
  below ~5 cases are not evidence.**
- Generic failure modes still visible:
  1. Submitting a 0-row result without testing. A candidate fix is a stateless
     `allow_empty` flag on `submit_answer`, so 0-row results are rejected unless
     explicitly confirmed.
  2. The NULL `<>` pitfall persists as a prompt-only rule. A candidate fix is a
     validator that flags `col <> 'literal'` on nullable columns.

---

## v5 — Pre-submit lint with confirmation (implemented, evaluated: 2 hard runs)

### Current result
- v4: hard mean 55.0/64 (85.9%) over 2 runs. Within-version noise is ~10 flips.

### Findings (motivating v5)
Two generic "runs fine, silently wrong" patterns from v4 run 2:
- #13 submitted an untested query that returned **0 rows**.
- #56 used `charge_code <> 'RF'`, which drops NULL codes. A prompt rule about this has
  existed since v3a, but prompt-only rules are followed stochastically.

### Change
- New `tools/sql_lint.py::lint_null_comparisons(query)`: uses sqlglot to find
  `col <> literal`, `col != literal`, and `col NOT IN (...)`. It resolves each column to
  its table (via aliases, skipping CTEs) and warns only if that column **actually
  contains NULLs** in the database. The warning text gives the row count and suggests
  `IS DISTINCT FROM`.
- `tools/submit_answer.py`: after the v3a error check, warn on NULL-lint hits or a 0-row
  result. The submission is not accepted until the agent either fixes the query or
  resubmits with `confirmation_code`, a hash derived from the query that the warning
  provides. This is not a hard block, because dropping NULLs or an empty answer can be
  intended.
  - The first attempt used `confirmed: bool`. In a smoke test the model set
    `confirmed=true` pre-emptively on its first submit (2/4 runs), which bypassed the
    check. A code that is only obtainable from the warning fixes that and stays
    stateless.
- Offline validation (no API cost):
  - **0/64 gold queries trigger the NULL lint, and 0/64 gold results are empty**, so the
    check never pushes back on a correct gold-style answer.
  - Over all past hard submissions, 10 would be flagged: 7 failed answers and 3 passed
    answers. The passed ones just need a confirmation.
  - Only 1 past submission returned 0 rows (#13, failed).
- Tests: NULL warning → wrong code rejected → correct code accepted; empty-result
  warning; no warning on a column without NULLs. 18 tests pass.
- Smoke test after the code fix (#56 ×2, #13 ×2): 3/4 PASS, and no pre-emptive bypass
  attempts. The lint did not fire in these runs because the model wrote NULL-safe
  queries. The #13 failure was a non-empty wrong answer.

### Expected effect
- Small: it targets ~1–3 cases per run. Given ~10-flip noise, 2 runs are needed to
  separate it from noise.

### Result after change (2 hard runs, run sequentially)
- **Run 1 (`run_20261125_221127`): 54/64 (84.4%). Run 2 (`run_20261125_221513`): 52/64
  (81.2%). Mean 53.0/64 (82.8%)**, vs v4 mean 55.0 (58/52). Both versions show 10
  within-version flips, so the −2 difference is inside the noise. Zero infra errors.
  Both runs were labeled after the fact (`summary RUN --label v5`).
- **The lint fired 3 times across the 2 runs and behaved as intended:**
  - #62: `res.position <> 0` on a column with 10,529 NULLs. The agent judged that
    excluding NULL positions was intended, resubmitted with the code, and **passed**.
    The check did not push it off a correct answer.
  - #56: `charge_code <> 'RF'`. The agent confirmed with the code and passed.
  - #3: `k_symbol <> 'UROK'`. The agent rewrote the query, but #3 fails regardless
    (wrong schema).
  - The empty-result warning never fired.
  - **No measurable effect on pass rate, and no harm observed.**
- The one non-mismatch failure (run 1, MAX_ITERATIONS) is unrelated to v5. It is a
  **repetition loop**: the agent picked `Countries` instead of `world`, then ran the
  same `SELECT DISTINCT "Indicator Name" ... ILIKE '%population%'` 28 times until the
  30-turn budget ran out. A candidate fix is framework-level detection of identical
  repeated tool calls.

### Stability over all 7 hard runs (v2, v3a, v3b, v4×2, v5×2)
- Pass counts: 51, 52, 55, 58, 52, 54, 52.
- Per case: **33 always pass**. 14 fail once, 7 fail twice, 4 fail three times, 2 fail
  four times. **Only 4 cases fail ≥5 of 7: #0, #3, #7, #53.**
  - #0 and #53: airline "completed" definition.
  - #3: ambiguous domain, wrong schema.
  - #7: "performing" = status A missed.
- The mean over the last 5 runs (v3b–v5, all with structural guide exposure) is
  **54.2/64 (84.7%)**, within the 80–90% target. The practical spread for one run is
  52–58.

### Decision
- Keep v5: it is generic, safe (0/64 gold flagged; it never pushed the agent off a
  correct answer), and cheap. Report it as a defensive check with no measurable
  effect at this sample size.

---

## Final run (v5 code, both splits)

- `run_20261125_221943`, label `final`, terminal output in `logs/eval_final.log`.
- **Easy 60/64 (93.8%), hard 56/64 (87.5%).** Zero infra errors and no 402s. Tokens
  5.03M in / 0.20M out. The hard score sits in the upper half of the 52–58 single-run
  range observed for v3b–v5, consistent with the ~84.7% multi-run mean.
- Easy failures: **#0, #3, #7, #53**. Hard failures: #0, #3, #5, #7, #12, #48, #51
  (MAX_ITERATIONS, the same repetition loop seen in v5 run 1), and #53.
- **Key finding: the 4 easy failures are exactly the 4 cases that fail in ≥5 of 7 hard
  runs.** In easy these rules are written into the question itself, so these failures
  are *not* retrieval problems. The model does not translate a known rule into the
  right SQL:
  - #0, #53: "completed = DepTime and ArrTime present" is dropped.
  - #7: "performing = status A" is dropped in an aggregate.
  - #3: the bare question "average transaction value" maps to the wrong look-alike
    schema.
  So guide retrieval is essentially solved for this set. The remaining ceiling is rule
  *application* and schema ambiguity, which points to reasoning effort or
  self-verification as the next levers, not more retrieval.

---

## Later iterations (sketch)

| Ver | Change | Target |
|-----|--------|--------|
| v5 (candidates) | Reasoning effort `high` (targets "rules in context but misapplied"; output tokens are only ~3% of cost); `first_token_timeout` check (deferred until ~70%, now reached); context compression for token cost; final full both-split run | Hard → 85–90% stable |

Noise: temperature 0.6 and 64 cases (1 case ≈ 1.6%). Run key versions ≥2 times and
report a range.

Anti-overfitting rules: never copy rule text from the easy prompts into the system
prompt; no schema-specific logic.
