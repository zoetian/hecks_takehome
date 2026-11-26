# Hecks NL→SQL Agent: Write-up

**Final result** (default model `openai/gpt-oss-120b:nitro`, final run on the last commit):

| Split | Baseline | Final |
|---|---|---|
| Easy | 7/64 (10.9%) | **60/64 (93.8%)** |
| Hard | 0/64 (0.0%) | **56/64 (87.5%)** |

Over the last five hard runs with the final architecture, the mean is **84.7%** and a
single run lands between 52/64 and 58/64. The number to quote is "mid-80s, ±5 cases per
run", not a single score. Why is explained under Finding 3.

The detailed working log, with per-iteration findings, failure tables, and every run,
is in [dev-notes.md](dev-notes.md). This document is the distilled version.

---

## 1. Approach

The starting agent had a single tool, `submit_answer`. It could not see the schema, run
SQL, or read the business guides. I worked in short iterations, each driven by failure
analysis of the previous run:

**measure → classify failures → fix the largest generic failure class → re-measure.**

Constraints I held throughout:

- `evaluate.py` is untouched except `create_tools()`. Guide files are untouched.
- No hand-written schema map and no rules copied from the eval prompts. Everything the
  agent knows about structure and rules is **discovered at runtime** from
  `information_schema` and from whatever markdown files sit in the guides directory.
- One commit per iteration, with the result and rationale in the commit body.

## 2. Iterations

| Ver | Change | Easy | Hard |
|---|---|---|---|
| v0 | Baseline (+ surface API error bodies) | 7/64 | 0/64 |
| v1 | Schema discovery + SQL tools (`list_schemas`, `describe_table`, `run_sql`); fix a tool-call streaming bug | 57/64 | 20/64 |
| v2 | Guide index in the system prompt + `read_guide` / `search_guides` | 54/64 | 51/64 |
| v3a | Pre-submit SQL execution check; search returns full guides (better ranking); generic SQL-pitfall rules | 55/64 | 52/64 |
| v3b | **Schema-linked guides auto-attached by `describe_table`** + rule checklist | — | 55/64 |
| v4 | Temperature 0.6 → 0.2 (2 runs) | — | 58, 52 |
| v5 | Pre-submit lint for NULL-dropping `<>` and empty results, with confirmation code (2 runs) | — | 54, 52 |
| final | v5 code, both splits | **60/64** | **56/64** |

(— = not run, to save budget. From v3b on I evaluated hard only.)

**What each step was about:**

- **v1: give the agent eyes.** In the baseline, 44–49/64 failures were hallucinated
  table names. Another 4–15 were the model trying to *explore* by submitting
  `information_schema` queries as its final answer, because submit was its only tool.
  The three discovery tools return compact output: column types, row counts, sample
  rows, and `''` rendered visibly so it is distinct from `NULL`. Easy jumped to 89%.
- **v2: give it the rules.** In v1, 41 of 44 hard failures used the *correct* schema
  with the wrong business definition (what "completed", "performing", or "legacy"
  means). A 2k-token guide index (title + section headings, auto-extracted) goes into
  the system prompt through a small framework hook (`Tool.prompt_context`), so the
  agent always knows the guides exist. Guides are read in full, not chunked, because
  the decisive definitions often sit in a trailing "Aggregation definitions" or "Scope"
  section. Hard jumped 31% → 80%.
- **v3: make rule use reliable.**
  - v3a fixed the specific v2 failure classes (untested SQL, reading only search
    snippets, the `col <> 'X'` NULL pitfall).
  - v3b is the structural fix. A guide is *linked* to a schema when its title names the
    schema or its text references `schema.table`. This is inferred, not mapped by hand,
    and it links all 12 eval schemas to exactly the right guide with no false links.
    `describe_table` attaches the linked guide, so the rules arrive with the table
    structure and the model no longer has to decide to look for them. 63/64 traces
    then contained the right guide.
- **v4/v5: consistency and silent-wrong-answer checks.** Neither produced a measurable
  gain (see Finding 3), but both are kept: they are generic and showed no harm.

## 3. Key findings

### Finding 1: Easy vs hard is purely "are the rules in the prompt?"

The two splits are the **same 64 questions with the same gold queries**. The easy prompt
appends the relevant business rules; the hard prompt omits them. That makes the
easy-to-hard gap a clean measurement of *rule retrieval*. It also gives a debugging
oracle: for any hard failure, the extra easy text is exactly the rule the agent should
have found. I used this only in the debug tool, never in the agent.

### Finding 2: Retrieval is essentially solved; the remaining ceiling is rule *application*

**This is the most important result.** In the final run, the 4 easy failures are
#0, #3, #7, #53. These are **exactly** the 4 cases that fail in ≥5 of the 7 hard runs.
In easy the rules are written into the question itself, so these are not retrieval
failures. The model knows the rule and still does not turn it into the right SQL:

- #0, #53 (airline): the question says "completed flights have departure *and* arrival
  times". The agent filters only `Cancelled = 0`.
- #7: "performing loans = status A" is dropped inside an aggregate.
- #3: "average transaction value" with no domain named. The agent picks a look-alike
  schema (`financial.trans` instead of `Credit.charge`). This is partly genuine
  ambiguity in the question.

**Implication:** more retrieval work (better search, embeddings, reranking) would not
move these. The next levers are on the reasoning side: higher reasoning effort, or an
explicit self-verification step that checks the final SQL clause-by-clause against the
stated definitions.

### Finding 3: Run-to-run variance dominates single-run comparisons

After v2, version-to-version deltas became smaller than the noise:

- Two runs of **identical code** (v4, and again v5) differed on **10/64 hard cases**. A
  single run's spread is about ±3–5 cases.
- Across 7 hard runs (v2–v5): **33 cases always pass and only 4 fail consistently**.
  The other ~27 flip at least once.
- Lowering temperature 0.6 → 0.2 did *not* measurably reduce this: still 10 flips.
  Plausible other sources, not verified: `:nitro` routing to different providers, and
  long multi-turn trajectories amplifying small early differences.

**Consequences for how I worked:**

1. A single-run delta of under ~5 cases is not evidence. v4 and v5 are reported as
   "no measurable effect" rather than as wins or losses.
2. Versions are compared on means over ≥2 runs.
3. Prompt-only fixes are unreliable. The model follows a prompt rule *sometimes*. The
   changes that clearly worked were **structural**: tools whose output puts the right
   information in front of the model whether or not it decided to ask. Examples are
   auto-attached guides, and a submit tool that rejects failing SQL.

### Finding 4: Prompt-level "please confirm" is bypassed; make it unforgeable

v5's lint asks the agent to confirm before submitting a query that silently drops NULL
rows. A boolean `confirmed=true` flag was set *pre-emptively* on the first submit in
2/4 smoke runs, which defeated the check. I replaced it with a short code derived from
the query hash that only the warning reveals. The check stays stateless and safe under
concurrency, and it can no longer be skipped. In the eval runs, the agent correctly
kept an intentional `<>` twice (both passed) and rewrote one query. Offline, 0/64 gold
queries trigger the lint, so the check never pushes the agent away from a gold-style
answer.

## 4. Unexpected problems: infra, cost, and measuring efficiently

A large share of the effort went into problems that were not about the agent at all.

**Framework bugs found through error bodies.** v0 changed the HTTP client to keep error
response bodies. That exposed a real bug: one provider (Nebius) streams the tail of a
tool call's arguments under a *new index with an empty id*. The original accumulator
then produced invalid JSON plus a phantom call. The next request failed with
`400: tool messages must include a non-empty tool_call_id`, which killed the episode.
This was the true cause of the "transient" baseline 400s. It is now fixed and covered
by unit tests.

**Cost is dominated by input tokens.** A full both-split run is about 5M input vs 0.2M
output tokens (~25:1), because the whole history is resent every turn. Two cost lessons:

- `max_tokens=100000` caused HTTP 402 `in_flight_budget_exhausted` on ~95% of requests.
  OpenRouter reserves credit per in-flight request at the *maximum* possible cost, and
  16 workers × 100k reserved far more than the account balance. Actual completions:
  median 169, p99 945, max 1,563 tokens. Capping at 16k, and retrying only this
  specific 402 with its Retry-After, fixed it. A key with "no limit set" can still hit
  this, because the in-flight budget is bounded by the credit balance, not by the
  key's spending limit.
- Three eval runs were wasted on this before it was understood. Cheap offline checks
  (reading traces) diagnosed it without spending more.

**Evaluating on a budget.**

- From v3b on I ran **hard only**: half the cost, and it is the metric that matters.
  One both-split run at the end serves as the final result.
- I considered re-running only previously-failing cases, but rejected it. With ~27
  flaky cases, failing cases regress toward the mean on rerun, so a subset run would
  overstate any fix.
- New mechanisms were first validated **offline against existing traces and gold
  queries**, which costs nothing. For example, the lint was checked for false positives
  on all 64 gold queries, and guide-to-schema linking was checked on all 12 schemas.
  Then a 2–4 case smoke test confirmed end-to-end behavior before spending a full run.

## 5. Tooling: `scripts/debug_eval.py`

The eval script reports pass/fail, but not *why*. To analyze failures quickly and keep
a record of every run, I built a read-only debugger over the trace files. It never
touches `evaluate.py`.

- **`summary`** re-executes gold and submitted SQL and auto-classifies each failure:
  `WRONG_SCHEMA`, `ROWCOUNT`, `MISSING_COLUMN`, `VALUES_DIFFER` (naming the wrong
  column), `ROW_ASSOCIATION`, `SQL_ERROR`, `MAX_ITERATIONS`, `INFRA`. Column matching
  reuses the evaluator's own tolerance logic.
- **`case N`** gives a one-screen diagnosis of one case:
  - the question, plus the rules the agent had to find (the easy-prompt diff);
  - WHERE/HAVING/JOIN predicates present only in gold or only in the submission
    (via sqlglot);
  - side-by-side SQL;
  - rows present on only one side;
  - the tool-call trajectory, with optional per-turn reasoning.

  Example: one case showed `+ charge_amt <> 0.01` missing and `3684257.75` vs
  `3684257.78` in the row diff. That is the "$0.01 test transaction" rule, found in
  seconds.
- **`diff`** auto-compares the latest run against the previous *version*
  (label-aware), listing fixed, regressed, and still-failing cases with diagnoses.
- **`history`** gives pass rates and token totals for every run.

Every report is saved next to the run's traces, so the run history is reproducible
locally. The stability analysis in Finding 3 came directly from this data.

## 6. Generality and overfitting

- **Discovered, not hard-coded.** Schemas, tables, and columns come from
  `information_schema`. The guide index, the guide-to-schema links, and search all work
  on any directory of markdown files (override with `HECKS_GUIDES_DIR`). Nothing
  references a specific schema, guide, or question.
- **Rules come from the guides only.** No text from the easy prompts was copied into
  prompts or code. The easy/hard pairing is used only in the debug tool.
- **Checks are semantic and generic.** Pre-submit execution, the NULL `<>` lint (fires
  only when the column actually contains NULLs), and empty-result confirmation are
  general SQL hygiene. They are not tuned to these answers.
- **Known scaling limit.** The full guide index lives in the system prompt (~2k tokens
  for 49 guides). With hundreds of guides this should become retrieval over the index,
  with schema-linked attachment kept as the primary path.

## 7. What I would do next

In order of expected value:

1. **Reasoning effort `high`** for gpt-oss. It targets Finding 2 directly, and output
   tokens are only ~4% of cost.
2. **Self-verification before submit.** Have the model restate each definition the
   question or guide gives, then map each to a clause in the final SQL and re-check.
   This should be enforced through the submit tool, not only asked for in the prompt.
3. **Loop detection.** The one recurring `MAX_ITERATIONS` failure is the agent
   re-running the same query 28 times. The framework should intervene on identical
   repeated tool calls.
4. **Cheaper turns.** Enable the framework's context compression for old tool results,
   and avoid re-sending large `list_schemas` and guide outputs. Input tokens are 96% of
   cost.
5. **Better measurement.** Use ≥3 runs per version and report per-case pass
   frequencies instead of single scores.
