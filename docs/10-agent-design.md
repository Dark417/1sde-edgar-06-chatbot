# Agent design — natural language over the EDGAR gold marts

> Authoritative for repo 6. Where this and `AGENTS.md` disagree, report it.
> Companion docs: `00-design-doc.md` (project), `02-data-contracts.md` (§4 gold).

## 1. The problem

Gold holds four marts and six views describing 500 companies' filings and XBRL
facts, including a bitemporal `financial_fact` grain that makes restatement
detection a query. A user should be able to ask for any of it in English and get
an answer they can verify.

The hard part is not making an LLM talk about finance. It is making sure that
every number it says is **true, current, and traceable** — because a chatbot that
is confidently wrong about revenue is worse than no chatbot at all.

## 2. The governing decision: the model plans, tools compute

```
                 model decides WHICH question          tools decide WHAT the answer IS
    user ──► [ route ] ──► [ plan ] ──► [ execute ] ──► [ synthesize ] ──► answer
                                            │                  ▲
                                     DuckDB / SQL              │
                                     over gold Parquet ────────┘
                                     (deterministic, cited)
```

Every figure originates in a fixed SQL statement inside a typed Python function.
The model chooses *which* function and *with what arguments*, then writes prose
around results it did not compute. It never sums, ranks, filters by date, or
converts units.

**Why this and not text-to-SQL.** Generated SQL over this schema is dangerous in a
specific, non-obvious way: `financial_fact` is bitemporal, so a query that forgets
to scope `period_type` will happily compare a quarter to a fiscal year, and one
that forgets `accession_number` will double-count a restated fact. The result is
not an error — it is a plausible number. Fixed tools encode those joins once,
correctly, and are unit-tested. A constrained SQL escape hatch exists (F-8) for
the long tail, but it is off by default and its answers are labeled.

**Why not RAG.** There is no text corpus here. The data is structured and numeric;
"find the right rows" is a `WHERE` clause, and embeddings would make an exact
operation approximate. Vector stores are explicitly forbidden in this repo. (If
filing *bodies* were ever ingested, that changes — and it would be a repo 3
decision first.)

## 3. Where it reads from, and why not Databricks

Same reasoning as repo 5 (§5.4 of the design doc): Free Edition compute shuts down
for the rest of the day on quota exhaustion, and the demo must survive that. The
bot reads the Parquet export from S3 via DuckDB and has **no runtime dependency on
Databricks at all**.

Consequence worth stating plainly: the bot's answers are as fresh as the last
export, not as fresh as the workspace. That is disclosed in every response header
and enforced by `/health` returning 503 past 48 hours. Visible staleness is
honest; hidden staleness is a lie.

## 4. Context engineering

The four assets in `context/` do more for answer quality than any framework or
model choice. They are versioned files with tests, never inline strings.

| Asset | Answers the question | Failure it prevents |
|---|---|---|
| `schema_card.md` | What *is* a filing, a fact, a restatement? What is the grain? | The model treating `financial_fact` as one-row-per-period and quietly ignoring restatements |
| `metrics.yaml` | What does "revenue", "profitable", "growth" mean *here*? | Invented definitions that drift between answers in the same conversation |
| `examples.yaml` | What does a good tool plan look like? | Tool-call flailing on the first ambiguous question |
| `policy.md` | What must I refuse, disclaim, and cite? | Investment advice; `materiality_band` presented as an accounting standard |

**Budget discipline:** the whole system prompt has a declared token budget with a
test that fails when it is exceeded. Context is a scarce resource, and an
ever-growing prompt is how latency and cost quietly triple.

**Explicitly state the negative space.** The schema card lists the questions the
data *cannot* answer — no filing text, 15 concepts only, ~500 companies, daily
batch. Models hallucinate most readily where they do not know a boundary exists.

## 5. Routing

A cheap first pass classifies the turn, because most turns do not need the
expensive path and some must not reach it at all.

| Class | Path |
|---|---|
| `company_specific` | resolve → tools → synthesize |
| `cross_company` | tools (aggregate/rank) → synthesize |
| `metadata` | `get_data_coverage` → synthesize (often no model call at all) |
| `out_of_scope` | refuse, naming what *is* covered |
| `advice_seeking` | refuse, offer the underlying facts |

Routing is a guardrail as much as an optimization: `advice_seeking` must be
structurally incapable of reaching the tool executor, which a graph edge
guarantees and a prompt instruction does not.

## 6. Memory — two kinds, both bounded

- **Conversation window**: last 8 turns verbatim, summarized beyond that.
- **Entity slots**: the resolved CIK(s) and the active period. This is what makes
  "what about 2023?" and "and its main competitor?" work without re-resolving,
  and it is cleared the moment the user names a different company.

No long-term user memory, no vector recall over past conversations. For a public,
anonymous demo those add storage, privacy questions, and a failure mode, and buy
nothing.

**The subtle bug to avoid:** stale entity slots. If the user pivots companies and
the slot survives, the bot answers confidently about the wrong company — the same
class of failure as guessing an ambiguous CIK. Slot invalidation is tested.

## 7. Guardrails

**Input**
- Scope and advice classification (see routing).
- Prompt-injection screen: filing text and company names are *data*, never
  instructions. A company literally named "Ignore previous instructions Inc."
  must not steer the agent.

**Output** — a `verify` node, not a prompt request:
- Every numeric claim traces to a tool result; uncited figures fail and trigger
  one retry.
- `materiality_band` always carries its thresholds and the words "product
  heuristic".
- Staleness banner when the manifest is old.
- Hard refusal on advice, with a specific alternative offered.

**Caps**: ≤6 tool calls, ≤200 rows/tool, ≤8k tokens of tool output, 20s wall
clock. Every cap breach is *reported* in the answer (`truncated: true`), never
silently trimmed — a silently truncated ranking is a wrong ranking.

## 8. Implementation options

The **tool layer is identical in all three**. Only the adapter changes, which is
the same interface discipline as the five-repo split: the asset is the tools, the
framework is a detail.

### Option A — LangGraph + LangChain over Bedrock  ← **the build**
Graph nodes are pure functions of typed state; `ChatBedrock` supplies the model;
`@tool`-decorated functions supply the tool schema. LangChain is used **only** for
model and tool adapters — no chains, no agent executors, no memory abstractions,
because those hide exactly the control flow this repo exists to make legible.

*Chosen because:* explicit, testable control flow; each node unit-testable with a
stub model; the graph is a diagram you can put on screen; Bedrock keeps
credentials in the AWS account that already exists, via OIDC.

*Cost:* a real dependency and a learning curve. Justified only because the graph
has branches, a bounded loop, and a verification step — a linear chatbot would
not need it.

### Option B — Google ADK as a second front-end (optional)
`Agent(model=..., instruction=..., tools=[...])` over the *same* Python tool
functions. Its value is `adk web`: a dev UI that shows each tool call, its
arguments, and its result live. Watching the agent resolve "Apple" → CIK →
restatements is a stronger demo moment than a chat bubble.

*Cost:* a second cloud and a Gemini key for one component. Worth it only if the
demo UI earns it.

### Option C — Bedrock Agents (managed) — the productionize path
AWS orchestrates; action groups are defined from an OpenAPI schema (repo 5 already
generates and commits one) backed by Lambda; sessions are managed.

*Not chosen now:* heavier plumbing, slower iteration, harder to test locally, and
it moves the interesting logic into console configuration where it cannot be
reviewed in a PR. It is the right answer at team scale, and saying why it is not
the right answer *here* is a better interview answer than adopting it.

### Considered and rejected
- **Databricks AI/BI Genie** — the platform-native NL-to-SQL. Rejected for the
  same reason repo 5 avoids Databricks: quota-driven shutdown would kill the
  public demo. Worth naming, because knowing the native option exists and having
  a constraint-based reason to skip it is the point.
- **A bare tool-calling loop** (Bedrock `converse` + 30 lines) — genuinely
  sufficient, and the right call if LangGraph proves to be friction. Keep this in
  your pocket: if the graph starts costing more than it explains, delete it.

## 9. Abuse, rate limiting, and cost containment

Repo 5 serves DuckDB reads that cost effectively nothing, so a traffic spike there
is a performance problem. **This repo spends real money per request**, so a spike
is a bill, and a scripted abuser is an unbounded one. The two failure modes are
different and need different controls:

- **Availability** — a dozen testers, or one loop, making the bot unusable for
  everyone else. Fixed by rate limiting.
- **Cost** — a slow drip of requests under any rate limit, running all month.
  Fixed by a hard budget ceiling. **Rate limiting alone does not protect the
  wallet**, and this is the mistake worth not making.

### What a turn actually costs

Roughly 5k input tokens (system prompt ≈ 3k: schema card, metrics, examples,
policy; tool results ≈ 2k) and ~400 output. At small-model prices that is around
half a cent a turn; at mid-tier, a couple of cents. So:

| Scenario | Turns | Order of cost |
|---|---|---|
| 12 testers × 20 turns | 240 | a few dollars |
| One scripted loop overnight | 10,000+ | hundreds |

The design target is therefore: **generous to humans, hostile to loops.**

### Layered controls, cheapest first

**L0 — Hard budget ceiling (do this first).** An AWS Budget alarm is a *notice*,
not a stop, so the app also keeps its own daily token/turn counter and flips into
a read-only "demo limit reached for today" mode when it trips. Degraded, honest,
and free is better than a surprise invoice.

**L1 — Structural, already in the design.** Several rules written as correctness
guardrails double as cost controls, which is why they were cheap to adopt:
routing refuses out-of-scope and advice-seeking turns *before* the expensive model
runs; ≤6 tool calls; ≤8k tokens of tool output; a hard `max_tokens` on output; a
20s wall clock. An out-of-boundary question should cost a router call and nothing
more.

**L2 — Application rate limits (the reliable layer).** In-process token buckets,
because the app is the only place that sees a *turn* rather than an HTTP request:

| Limit | Suggested start |
|---|---|
| Per-IP burst | 5 turns / 10 min |
| Per-IP daily | 20 turns / day |
| Per-session | 30 turns, then a fresh-conversation prompt |
| Global concurrency | 4 in-flight model calls (a semaphore) |
| Global daily | ~300 turns, then degraded mode |
| Input length | reject > 500 characters before any model call |

Key the buckets on **IP + an anonymous session id**, not IP alone — mobile and
corporate NAT put many real users behind one address. The global concurrency
semaphore matters more than it looks: it is what stops a burst from tripping
Bedrock's account-level throughput quota and turning one abuser's traffic into
`ThrottlingException` for everyone.

**L3 — Edge (optional, free).** Putting Cloudflare's free tier in front of Fly
gives bot detection, a rate-limiting rule, and static-asset caching before traffic
reaches the container. Worth doing if the link is ever posted publicly; not worth
doing for a handful of known testers.

**L4 — Response caching.** A demo receives the *same questions repeatedly*.
Caching normalized question → answer for a short TTL cuts both cost and latency
noticeably, and is safe as long as the cache key includes the manifest's
`generated_at` so a fresh export invalidates it.

Also: enable **prompt caching** on the model call if available. The system prompt
is byte-identical across every turn and is the majority of input tokens, so this
is the single largest cost reduction available for a context-heavy agent.

### Why not AWS-native rate limiting

API Gateway usage plans and WAF rate-based rules are the standard AWS answers, but
they sit in front of AWS-hosted endpoints. This bot deploys to Fly (design doc
§5.4 — the demo must survive Databricks *and* be trivially always-on), so putting
API Gateway or CloudFront+WAF in front means adding AWS hosting for the sole
purpose of throttling. Not worth it at this scale. Bedrock itself has no
per-caller rate limiting — its quotas are account-wide, which is precisely why the
app must self-limit rather than rely on the platform.

If this ever moved to AWS hosting, WAF rate-based rules plus API Gateway usage
plans would replace L2/L3 and the app-level limiter would stay as defense in
depth.

### Degrade, never fail

Hitting a limit returns a **200 with an explanation**, not a 500: what the limit
is, when it resets, and a link to repo 5's REST API and to a canned example
answer. A rate-limited demo that explains itself still demonstrates the product; a
stack trace does not.

Every turn is logged with token counts and estimated cost, so "what happened last
night" is answerable.

## 10. Evaluation

Model output is non-deterministic, so assert **properties**, not strings:

| Category | Assertion |
|---|---|
| Factual lookup | correct entity, figure matches the tool result, citation present |
| Ranking | correct ordering, `truncated` disclosed |
| Ambiguity | asks a clarifying question, does not guess |
| Restatement | names both accessions, labels the band as a heuristic |
| Out of scope | refuses and names actual coverage |
| Advice-seeking | refuses, offers facts |
| Stale data | staleness disclosed |
| Injection | instruction inside data is ignored |

Scored in CI on a cheap model; a drop below threshold fails the build. Without
this, "it works" means "it worked the three times I tried it."

## 11. Open questions

1. **Model choice per node** — a small model for routing and a larger one for
   synthesis is the obvious split; measure before assuming the large one is
   needed for planning.
2. **Streaming vs. verification** — the citation verifier wants the whole answer
   before approving it. Streaming a response that is then retracted is worse than
   a one-second wait; likely resolution is to stream only after `verify` passes,
   or to stream the reasoning trace while the answer is checked.
3. **Whether F-8 (constrained SQL) ever ships** — decide after the evals show
   which real questions the fixed tools cannot reach.
