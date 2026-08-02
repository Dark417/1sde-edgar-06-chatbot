# Repo 6 / 6 — `1sde-edgar-06-chatbot`

> Copy to repo root as `AGENTS.md`. Sections 0–8 are agent instructions. Section 9 is
> yours, by hand. Section 10 is what the project close-out needs.
>
> GitHub: `github.com/Dark417/1sde-edgar-06-chatbot`
> Build order position: **6 of 6.** Requires repo 1 published and repo 4's gold
> export present. Does **not** require repos 2/3 to be running.

---

## 0. Read first

This repo is a natural-language interface over the gold marts: a user asks
"which companies restated revenue most materially this year?" and gets a correct,
**cited** answer. It is the second public surface after repo 5, and the one that
makes the bitemporal model legible to someone who will never read a schema.

**Authoritative docs** in `docs/`: `00-design-doc.md` (§5.4 why nothing here talks
to Databricks), `02-data-contracts.md` (§4 gold tables — the only data this repo
sees), and this repo's `docs/10-agent-design.md`.

**The one idea that governs this repo:** the model chooses *which question to ask
the data*, never *how to compute the answer*. Every number in every response comes
from a deterministic tool that ran a fixed query. If the LLM is ever the thing
doing arithmetic, aggregation, or filtering, the design has failed — that is how
you get a confident, wrong, unfalsifiable financial answer.

**Second idea:** an uncited number is a liability. Every factual claim carries the
asserting `accession_number` and a sec.gov link, so any answer can be checked
against the filing in one click.

---

## 1. Scope

### Owns
- The tool layer: a small set of typed, read-only functions over the gold Parquet.
- Agent orchestration: routing, tool calling, conversation memory, synthesis.
- Prompt/context assets: schema card, metric dictionary, few-shot examples.
- Guardrails: scope refusal, no-advice policy, citation enforcement, caps.
- A minimal chat UI and its deployment.

### Does NOT own
- Any data transformation. If an answer needs a shape gold does not have, the fix
  belongs in **repo 4**, not in a tool, and never in a prompt.
- Any Databricks connection. See rule 1.
- Any write path. No route, tool, or SQL statement mutates anything.
- Schema definitions. Import them from `edgar_lakehouse_contracts`.
- The REST API in repo 5. This repo may *reuse repo 5's query layer pattern*, but
  it does not modify repo 5.

### The boundary that will tempt you
"The model could just write the SQL." Text-to-SQL over a finance dataset produces
answers that are plausible, unverifiable, and occasionally catastrophic — a
mis-scoped `period_type` silently compares a quarter to a year. Fixed tools first.
A constrained SQL escape hatch is F-8, gated, capped, and off by default.

---

## 2. Prerequisites

| Input | Source | Used for |
|---|---|---|
| `edgar-lakehouse-contracts==<version>` | repo 1 release wheel | gold column names/types, concept set |
| `s3://<serving>/v1/{table}/data.parquet` | repo 4 export | the only data source |
| `s3://<serving>/v1/_manifest.json` | repo 4 | freshness, row counts, `contracts_version` |
| `/edgar-lakehouse/s3/serving_bucket` | repo 2 SSM | bucket name at runtime |
| Model credentials | AWS Bedrock (IAM/OIDC) and/or Google AI Studio key | inference |

**Gate before generating:** confirm the export exists and `restatement_event` has
rows. A chatbot whose flagship question returns "no data" is worse than no chatbot.

```bash
aws s3 cp s3://<serving>/v1/_manifest.json - | jq '.row_counts'
```

---

## 3. Tech baseline

```
Python        3.11
Agent         LangGraph (orchestration) + LangChain (model/tool adapters)
Models        Bedrock (Claude) via langchain-aws; Google ADK/Gemini as a second front-end
Query         duckdb over the gold Parquet (same pattern as repo 5)
API           FastAPI + SSE for token streaming
Models/typing pydantic v2, mypy --strict
Tests         pytest, pytest-asyncio, syrupy or plain fixtures for prompt snapshots
UI            single HTML + vanilla JS. No build step, no node_modules.
Deploy        container on Fly.io (or Render) — always-on free tier
```

**Forbidden dependencies:** `databricks-sql-connector`, `databricks-sdk`,
`pyspark`, and any vector database. This repo has no embeddings and no RAG: the
data is structured and numeric, so retrieval is a `WHERE` clause, not a cosine
distance. Adding a vector store here is resume-driven development and will be
rejected in review.

**On LangGraph vs a bare loop:** LangGraph earns its place because the graph is
the artifact you can show, test, and reason about — nodes are pure functions,
edges are explicit, and state is typed. Use LangChain **only** for its model and
tool adapters (`ChatBedrock`, `@tool`), not for chains, agents, or memory
abstractions; those hide the control flow this repo is meant to make visible.

---

## 4. Layered structure

```
1sde-edgar-06-chatbot/
├── AGENTS.md
├── pyproject.toml
├── Dockerfile
├── fly.toml
├── src/chatbot/
│   ├── config.py            # L0: settings, SSM resolution
│   ├── store.py             # L1: DuckDB over gold Parquet, TTL refresh
│   ├── tools/
│   │   ├── base.py          # L1: ToolResult envelope (data + citations + caveats)
│   │   ├── resolve.py       # L2: company name/ticker -> CIK
│   │   ├── company.py       # L2: profile, filings, financials
│   │   ├── restatements.py  # L2: the flagship
│   │   ├── market.py        # L2: cross-company aggregates
│   │   └── meta.py          # L2: freshness, coverage, capabilities
│   ├── context/
│   │   ├── schema_card.md   # what the data IS (hand-written, versioned)
│   │   ├── metrics.yaml     # business term -> concept + calculation
│   │   ├── examples.yaml    # few-shot question -> tool plan
│   │   └── policy.md        # refusals, disclaimers, citation rules
│   ├── graph/
│   │   ├── state.py         # L3: typed graph state
│   │   ├── nodes.py         # L3: route / plan / execute / synthesize / verify
│   │   └── build.py         # L3: LangGraph wiring
│   ├── memory.py            # L2: conversation window + resolved-entity slots
│   ├── guardrails.py        # L2: input scope check, output citation check
│   └── app.py               # L4: FastAPI + SSE, wiring only
├── static/                  # index.html, app.js, style.css
├── evals/                   # golden question set + scoring
├── tests/
└── .github/workflows/ci.yml
```

**Layer rule:** tools know nothing about LLMs; the graph knows nothing about SQL;
`app.py` contains no logic. A tool that takes a `messages` argument, or a node
that builds a SQL string, is a review failure.

---

## 5. Non-negotiable rules for the agent

1. **No Databricks import, ever.** Reads Parquet from S3, same as repo 5. A test
   asserts `databricks` is absent from `sys.modules`; CI greps `pyproject.toml`.
   Free Edition compute can vanish for the rest of the day and the demo must not.
2. **The model never computes a number.** Sums, ratios, deltas, rankings, and
   date filtering happen in SQL inside a tool. The model selects tools, passes
   arguments, and writes prose around results it did not calculate.
3. **Every factual claim is cited.** Tool results carry `accession_number` and a
   sec.gov URL; the synthesis prompt requires them inline; a post-check rejects a
   response containing a bare figure with no citation and retries once.
4. **Read-only, end to end.** No route other than GET/POST-for-chat, no SQL verb
   other than SELECT, no tool with a side effect.
5. **Every tool returns a `ToolResult`**: `data`, `row_count`, `truncated`,
   `citations`, `caveats`, `as_of`. A bare list is not acceptable — the model
   cannot honestly caveat what it cannot see.
6. **Hard caps:** ≤200 rows per tool, ≤6 tool calls per turn, ≤8k tokens of tool
   output per turn, 20s wall clock. Exceeding a cap is a *reported* condition
   (`truncated: true`), never a silent trim.
7. **Unresolved entities stop the turn.** If `resolve_company` returns 0 or >1
   plausible match, ask the user rather than guessing. Guessing the wrong CIK
   produces a confidently wrong answer about the wrong company.
8. **`materiality_band` is labeled a product heuristic** every time it appears,
   with thresholds. It is not an accounting standard, and a finance interviewer
   will catch a claim that it is.
9. **No investment advice, ever.** "Should I buy X" is refused with a one-line
   explanation and an offer to show the underlying facts. This is a data
   assistant, not an adviser.
10. **Out-of-scope questions are refused specifically**, naming what the dataset
    *does* cover and its bounds (CIK universe size, date range, 15 concepts).
    A vague "I can't help" reads as broken; a precise boundary reads as honest.
11. **Stale data is disclosed, not hidden.** If `manifest.age_hours > 48`, every
    answer carries a staleness banner and `/health` returns 503.
12. **Prompts are files, not string literals.** Everything in `context/` is a
    versioned asset with a test. A prompt edited inline in Python is invisible to
    review.
13. **No prompt contains a secret, a bucket name, or an account id.** Global rule
    on sensitive values applies to prompt assets exactly as to code.
14. **Every graph node is a pure function of state.** No node reads config from
    the environment or calls a model outside the injected client — otherwise the
    graph is untestable and the evals are theatre.

---

## 6. Features to generate

### F-1 · `config.py` + `store.py`
Settings resolved `env → SSM → error naming the key`. `GoldStore` registers one
DuckDB view per gold table plus the six v1.1.0 gold views, TTL refresh (default
15 min) behind a lock, `manifest` property.

**Acceptance:** missing Parquet → `manifest is None`, no exception at import;
concurrent requests during a refresh window trigger exactly one reload.

### F-2 · `tools/` — the whole product, really
```python
resolve_company(query: str)                          -> ToolResult   # name/ticker -> CIK
get_company_profile(cik: str)                        -> ToolResult
list_filings(cik, form_type=None, since=None, limit=50)  -> ToolResult
get_financials(cik, concept=None, period_end=None, limit=50) -> ToolResult
get_restatements(cik=None, band=None, since=None, limit=50)  -> ToolResult
compare_companies(ciks: list[str], concept: str, period_end: str) -> ToolResult
get_filing_activity(since=None, until=None, form_type=None)  -> ToolResult
get_data_coverage()                                  -> ToolResult   # freshness, bounds, universe
```

Each is a plain typed Python function with a docstring the model reads as the
tool description. **Write the docstrings for the model, not for a developer:**
state what the tool answers, what it does *not* answer, and the units of every
returned figure.

**Acceptance**
- Every tool has a contract test against fixture Parquet.
- Every returned money figure carries `unit` and the period it belongs to.
- `resolve_company("apple")` returns exactly one high-confidence match;
  `resolve_company("bank")` returns several and is flagged ambiguous.
- No tool returns more than 200 rows; truncation sets `truncated: true`.

### F-3 · `context/` — context engineering
- **`schema_card.md`** — one page: what a filing/fact/restatement *is*, the grain
  of each table, what the CIK universe covers, and the four questions the data
  genuinely cannot answer. Injected whole; it is small and it is the single
  highest-leverage asset in the repo.
- **`metrics.yaml`** — business phrase → concept + computation, e.g.
  `revenue → concept_canonical='revenue_total'`, `profitable → NetIncomeLoss > 0`.
  Without this the model invents definitions and they drift between answers.
- **`examples.yaml`** — 6–10 question → tool-plan pairs covering: single company
  lookup, ambiguous name, cross-company ranking, a restatement question, an
  out-of-scope question, and a question the data cannot answer.
- **`policy.md`** — refusals, the no-advice line, the materiality disclaimer,
  citation format.

**Acceptance:** a test renders the full system prompt and asserts it is under a
declared token budget; a test asserts every `metrics.yaml` concept exists in
`CONCEPT_SET` from the contracts package.

### F-4 · `graph/` — LangGraph orchestration
Five nodes, explicit edges:

```
        ┌──────────┐
input ─►│  route   │──(out_of_scope)─────────────► refuse ─► END
        └────┬─────┘
             │(in_scope)
        ┌────▼─────┐      ┌──────────┐
        │  plan    │─────►│ execute  │◄──┐  (loop, max 6 calls)
        └──────────┘      └────┬─────┘   │
                               │─────────┘
                          ┌────▼──────┐
                          │synthesize │
                          └────┬──────┘
                          ┌────▼──────┐
                          │  verify   │──(no citations)──► retry once
                          └────┬──────┘
                             END
```

- **route** — cheap/fast model or rules. Classes: `company_specific`,
  `cross_company`, `metadata`, `out_of_scope`, `advice_seeking`.
- **plan** — chooses tools and arguments; may ask a clarifying question instead.
- **execute** — runs tools, enforces caps, accumulates citations.
- **synthesize** — writes the answer from tool results only.
- **verify** — guardrail pass: citations present, no advice, caveats attached.

State is a typed `TypedDict`/pydantic model, checkpointed per conversation.

**Acceptance:** each node is unit-tested with a stub model; a graph-level test
asserts an `advice_seeking` question never reaches `execute`; a test asserts the
tool-call loop terminates at the cap.

### F-5 · `memory.py`
Two kinds, both bounded:
- **Conversation window** — last N turns (default 8), summarized beyond that.
- **Entity slots** — the resolved CIK(s) and period of the current thread, so
  "what about 2023?" and "and its competitor?" work without re-resolution.

No long-term/user-level memory, no vector recall. Slots are cleared when the user
names a different company.

**Acceptance:** a three-turn conversation resolves the company once, not
three times; changing company clears the slot.

### F-6 · `guardrails.py`
Input: scope classification, advice detection, prompt-injection screen (a filing
name is not an instruction). Output: citation presence, numeric-claim check
(every figure traceable to a tool result), disclaimer attachment, staleness
banner.

**Acceptance:** a synthesized answer containing an uncited number fails the
verifier; the advice-seeking prompt set is refused 100% of the time in evals.

### F-7 · `app.py` + `static/`
`POST /chat` (SSE token stream), `GET /health` (freshness, 503 when stale),
`GET /capabilities` (what the bot can answer — rendered from the schema card).
UI: single page, message list, streaming, a visible **freshness badge**, and an
expandable **"how I got this"** panel showing the tool calls and arguments.

That panel is the demo: it turns "chatbot" into "auditable data agent."

**Acceptance:** works with no build step; renders correctly when `/health` is
503; the tool-trace panel shows every call made for the answer on screen.

### F-8 · Constrained SQL escape hatch — **off by default**
`run_sql(query)` restricted to: SELECT only, gold views only, mandatory LIMIT,
5s timeout, EXPLAIN-checked before execution, feature-flagged.

Ship F-1..F-7 first. Enable this only after the eval suite is green, and label
its answers as generated SQL in the trace panel.

### F-9 · `evals/`
A golden set of ~30 questions with expected tool plans and assertions on the
answer (contains a citation, names the right company, refuses when it should).
Scored in CI. Categories: factual lookup, ranking, ambiguity, restatement,
out-of-scope, advice-seeking, stale-data, and prompt-injection.

**Acceptance:** eval score is printed on every PR; a drop below the threshold
fails the build. Model non-determinism is handled by asserting *properties*
(cited, correct entity, refused) rather than exact strings.

---

## 7. Testing requirements

| Requirement | Threshold |
|---|---|
| Coverage (tools, guardrails, memory, graph nodes) | ≥ 85% |
| Live model calls in unit tests | zero — stub the model client |
| Network in tests | zero — fixture Parquet on local disk |
| Every tool | contract test + truncation test |
| Guardrails | refusal set, citation set, injection set |
| Evals | run in CI against a cheap model; property assertions |
| No-Databricks test | required |

---

## 8. CI — `.github/workflows/ci.yml`

```
on: pull_request -> ruff, mypy, pytest, secret-scan, contract-compat,
                    grep gate (no databricks, no vector db), evals (cheap model)
on: push main    -> above + docker build + deploy to Fly
```

Model credentials come from OIDC (Bedrock) or a repository secret (Gemini). Evals
run on a small model to keep PR cost near zero; the deployed bot may use a larger
one.

---

## 9. EXECUTION — what you do manually

### 9.1 Confirm the data is worth talking to 🔴
```bash
aws s3 cp s3://<serving>/v1/_manifest.json - | jq '.row_counts'
```
`restatement_event > 0`, or the flagship question has nothing to show.

### 9.2 Pick and enable a model
- **Bedrock**: enable Claude model access in the console for `us-east-2`, confirm
  with a one-line `converse` call. Note that model access is per-region and
  per-model and must be requested before first use.
- **Gemini/ADK** (optional second front-end): an AI Studio key in `.env`.

### 9.3 Write the schema card by hand 🔴
The agent cannot know which parts of your data are trustworthy, what the CIK
universe actually contains, or which questions look answerable but are not. This
file is yours, not the generator's.

### 9.4 Build the golden eval set by hand 🔴
Pick real companies you know, including one with a real 10-K/A restatement and
one with none. Write the questions a recruiter would actually ask.

### 9.5 Run locally, watch the trace panel
```bash
uvicorn chatbot.app:app --reload
```
Ask the flagship question and read the tool trace. If the model computed anything
itself, fix the tool — do not fix the prompt.

### 9.6 Deploy and click your own link
Different device, cellular, logged out. Ask three questions. Check the freshness
badge and one citation link resolves to sec.gov.

---

## 10. Project close-out additions

| Output | Consumed by |
|---|---|
| Public chat URL | the front-door README, alongside repo 5's demo link |
| `evals/` results | evidence the thing works, not just that it runs |

**What to say about this in an interview:** lead with the constraint, not the
stack. "The model never computes a number — it picks a tool, and every figure in
the answer carries the accession number it came from. That is the difference
between a demo that impresses and one that a finance team could actually trust."
Then be ready for: why no RAG (structured data — retrieval is a WHERE clause),
why LangGraph over a bare loop (explicit, testable control flow), and how you
know it works (property-based evals, not vibes).

---

## 11. Definition of done

- [ ] `ruff`, `mypy --strict`, `pytest` green; coverage ≥ 85%
- [ ] No `databricks` dependency and no vector store (test + CI grep)
- [ ] Every tool has a contract test; every figure carries unit and period
- [ ] Model never computes: a test asserts arithmetic appears in no prompt path
- [ ] Citation verifier rejects uncited numbers
- [ ] Advice-seeking refused 100% in evals
- [ ] Ambiguous company names ask instead of guessing
- [ ] Stale data disclosed; `/health` 503 over 48h
- [ ] Tool-trace panel visible in the UI
- [ ] Eval suite green in CI with a published score
- [ ] Deployed, public URL live, clicked from another device

---

## 12. References

1. LangGraph — https://langchain-ai.github.io/langgraph/
2. LangChain AWS (`ChatBedrock`) — https://python.langchain.com/docs/integrations/chat/bedrock/
3. Bedrock Converse API + tool use — https://docs.aws.amazon.com/bedrock/latest/userguide/tool-use.html
4. Google ADK — https://google.github.io/adk-docs/
5. DuckDB over Parquet on S3 — https://duckdb.org/docs/stable/extensions/httpfs/s3api
6. Anthropic tool-use guidance — https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/overview
