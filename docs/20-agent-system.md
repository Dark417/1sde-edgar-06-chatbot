# 20 — Agent system design (authoritative)

> **This document supersedes `design1.md` and `10-agent-design.md`**, both now
> removed. It was written against the *live* environment on 2026-08-03 — every
> external reference below was verified by querying AWS, Databricks, or the
> repos, not copied from an earlier plan. Where it disagrees with any older
> document, this one wins.
>
> Companions: `21-test-plan.md` (test matrix), `SETUP-CREDENTIALS.md` (human
> setup), `docs/00-design-doc.md` + `docs/02-data-contracts.md` (project
> background, owned by repo 1 — kept as context copies only).

## 0. Corrections applied to the previous draft (design1.md)

Recorded so nobody re-introduces them:

| design1.md said | Verified reality |
|---|---|
| repo `Dark417/fin-lakehouse-chat` | this repo: `Dark417/1sde-edgar-06-chatbot` |
| SSM `/fin-chat/*`, `/fin-lakehouse/*` | prefix is **`/edgar-lakehouse/`**; this app owns **`/edgar-lakehouse/chat/*`** |
| `AWS_REGION=us-east-1` | **us-east-2** — global rule 13; the UC metastore lives there |
| gold `company_financials_current`, `filing_activity` | real tables: `financials_current`, `filing_activity_daily` (verified via `information_schema`) |
| `get_company_filings` tool over "filing_activity + profile" | no per-company filing table exists in the export; replaced (§4) |
| data at `s3://<serving>/v1/` | bucket `edgar-lake-serving` exists but is **empty** — repo 4's S3 export has not run. Source of truth today: `scripts/export_gold.py` → `data/*.parquet`. The store reads S3 when `SERVING_PREFIX` is set, local `data/` otherwise, same code path |
| Sonnet main / Haiku gate, ids from SSM | account cannot invoke **any Anthropic model** until the Bedrock use-case form is submitted (verified: `ResourceNotFoundException`). Model is **probed** from a candidate list; Amazon **Nova Pro/Lite work today**; SSM overrides win when set |
| ECS Fargate service + ALB, always-on | repo 2 *is* applied (state + SSM + OIDC verified), but an always-on Fargate task + ALB ≈ $30+/month against a $10 budget alarm. Infra ships **config now (SSM/secret), compute behind `deploy_chatbot=false`** (§8) |
| `make check` | Windows dev machine; `scripts/check.ps1` + `scripts/check.sh`, same steps in CI |

## 1. What this is

A chat interface over the EDGAR lakehouse **gold layer**. Users ask about
companies, reported financials, restatements, and filing activity in plain
English; every number in every answer comes from a deterministic SQL tool, and
each answer carries a visible trace of the tool calls that produced it.
Everything else is refused with fixed text.

**One UI, two interchangeable orchestrators** behind an `AgentRunner` seam:

| `AGENT_IMPL` | Orchestrator | Model access |
|---|---|---|
| `langgraph` (default) | LangGraph `StateGraph` (agent ⇄ tools, bounded) | `ChatBedrockConverse` (langchain-aws) |
| `adk` | Google ADK `LlmAgent` + `Runner` | LiteLLM `bedrock/<model>` |

Tools, store, guards, prompts, and UI are shared. The seam is proven by the
shared fake-model test suite passing under both values; if `google-adk` is not
installed the ADK suite **skips with a stated reason** — it must never fail the
build by absence, and never silently pass.

## 2. Targets

| # | Target | Verified by |
|---|---|---|
| G1 | Every numeric claim originates in a tool result; the UI shows which tools ran with args and row counts | loop tests + trace rendering test |
| G2 | The shared loop suite passes under `AGENT_IMPL=langgraph`; and under `adk` when installed | `pytest -m loop` (parameterized) |
| G3 | Out-of-scope → fixed `REFUSAL_TEXT`, ≤1 main-model round | boundary tests |
| G4 | Over-broad → fixed `TOO_BROAD_TEXT` at >3 tool rounds or >8,000 tool-output tokens | guard tests |
| G5 | Rate limits enforced: 10 msg/min and 60/day per session; global daily token budget; kill switch | guard tests |
| G6 | Zero write paths, zero Databricks runtime dependency, DuckDB read-only with external access disabled | store tests + grep gates |
| G7 | Local: `run.bat` → working chat at `localhost:8501` against exported data | manual §9 + smoke |
| G8 | Infra: SSM config + secret placeholder applied via repo 2 PR; compute deployable by one flag flip | terraform plan/apply |
| G9 | Fixed responses byte-identical across impls | constants test |

## 3. Architecture

```
              Streamlit ui/chat.py
   sidebar: impl toggle · model id · freshness · dataset counts
                    │ per message
                    ▼
            guard.check(session, budget, kill_switch)
                    │ pass                      │ fail → fixed text, no model call
                    ▼
        AgentRunner.run_turn(session_id, text) ─────────┐
        ┌─ langgraph ────────────┐  ┌─ adk ───────────┐ │
        │ StateGraph             │  │ LlmAgent+Runner │ │
        │ agent ⇄ ToolNode       │  │ callbacks:      │ │
        │ ≤3 rounds, 8k tok cap  │  │ guard + cap     │ │
        └───────────┬────────────┘  └────────┬────────┘ │
                    └────────── shared ──────┘          │
                    tools/ (8 typed, parameterized SQL) │
                    data/store.py DuckDB read_only      │
                    ── local data/*.parquet             │
                    ── or s3://edgar-lake-serving/v1/*  │
                    ▼                                   ▼
             gold Parquet                    TurnResult{text, tools_called,
             + _manifest.json                tokens_in/out, outcome}
```

`TurnResult.outcome ∈ {answered, refused, too_broad, budget, killed, error}`.
The UI and every test consume only `TurnResult` — neither knows which impl ran.

**Latency fact:** a chat round is model inference (1–4 s) + DuckDB (<300 ms).
Locally there is no cold start at all; in the flagged-off cloud deploy the
service is a warm ECS task.

## 4. Tool surface (complete — nothing else exists)

All tools are pure functions over the shared store, Pydantic-validated before
any SQL binds, every statement parameterized, `LIMIT` from a server constant.
Envelope: `{"rows", "row_count", "truncated", "caveats", "citations"}`.

| Tool | Params (validated) | Source |
|---|---|---|
| `list_companies` | — | company_profile |
| `search_companies` | `q: str(2..50)`, `limit<=20` | company_profile |
| `get_company_profile` | `cik: ^\d{1,10}$` | company_profile |
| `get_company_financials` | `cik`, `concept?: Enum(11)`, `fiscal_year?`, `limit<=50` | financials_current |
| `compare_companies` | `concept: Enum(11)`, `fiscal_year?` | financials_current |
| `get_restatements` | `cik?`, `band?: Enum(immaterial,notable,material)`, `concept?`, `limit<=50` | restatement_event |
| `restatement_summary` | — | restatement_event (fixed aggregates) |
| `get_filing_activity` | `form_type?`, `limit<=60` | filing_activity_daily |
| `get_data_coverage` | — | _manifest.json + counts |

The 11 concepts (verified from live gold): `revenue_total, net_income,
operating_income, gross_profit, assets_total, assets_current*, liabilities_total,
equity_total, cash_and_equivalents, eps_basic, eps_diluted, shares_outstanding`
— \*the Enum is generated **from the data at store load**, not hardcoded, and
the schema card lists it; a hardcoded list is how the last two column-rename
bugs happened.

Removed from design1: `get_company_filings` (no per-company filing table in the
export). Added relative to design1: `list_companies`, `compare_companies`,
`restatement_summary` — all three proven necessary by the working prototype
(rankings and "what do you have" are the most common questions).

## 5. Boundary policy

Three layers, cheapest first:

1. **Topic gate** (`TOPIC_GATE=on` default): the cheap model classifies
   `in_scope` before the main model runs. Off-topic → `REFUSAL_TEXT`, zero main
   tokens. When only one invocable model exists (today: Nova), gate and main
   share it; the gate prompt is ~50 tokens either way.
2. **Structural:** the system prompt requires answers only from tool results;
   a question no tool can serve → the model outputs exactly `OUT_OF_SCOPE`,
   which the runner maps to `REFUSAL_TEXT` (fixed text that lists what the
   system *can* answer — never a bare "I can't help").
3. **Breadth cap:** the loop halts with `TOO_BROAD_TEXT` when a 4th tool round
   would start or cumulative tool-output tokens exceed 8,000.

`REFUSAL_TEXT`, `TOO_BROAD_TEXT`, `BUDGET_TEXT`, `KILLED_TEXT`, `LIMIT_TEXT`
are constants in `prompts.py`. They are responses, not generations, and a test
asserts both impls emit them byte-identically. Investment advice is refused by
the structural layer with the figures offered instead — tested.

## 6. Limits, cost, kill switch

| Limit | Value | Keyed by | Mechanism |
|---|---|---|---|
| msgs/min | 10 | session id | in-process token bucket |
| msgs/day | 60 | session id | UTC-reset counter |
| model tokens/day | 200,000 (env `TOKEN_BUDGET_DAY`) | global | debited per round → `BUDGET_TEXT` |
| response size | `max_tokens=1024` | per call | model param |
| conversation | 40 rounds | session | hard stop + restart hint |
| kill switch | SSM `/edgar-lakehouse/chat/enabled` | global | checked per message, cached 30 s |

In-process buckets are valid because exactly one instance runs (locally by
definition; in cloud pinned `desired_count=1`, comment naming this section).
Per-IP buckets exist in code but activate only behind the ALB (locally
Streamlit cannot see a trustworthy client IP). Kill-switch lookup failure
**fails open locally** (no AWS ≠ dead demo) and **fails closed in cloud**
(`DEPLOY_ENV=cloud`).

Every message logs one structured line: session hash, impl, model id, tools
called, token counts, outcome. No question text is logged (it is user data).

## 7. Security constraints

- **SEC1** DuckDB `read_only` semantics: views over Parquet; after registration
  `SET enable_external_access=false`; tests prove `INSERT`, `ATTACH`, and
  `COPY TO` raise.
- **SEC2** No model-authored SQL anywhere. CI greps tool sources for f-strings
  containing `SELECT` — zero.
- **SEC3** Tool results enter the conversation wrapped in `<tool_data>` tags;
  the system prompt declares such content data, never instructions. Company
  names are attacker-influenced text; free-text fields capped at 200 chars.
- **SEC4** AWS auth is ambient (SSO locally, task role in cloud). No AWS keys
  in code, env files, or Streamlit secrets. The only secret material the app
  can ever read is the optional Anthropic key at
  `/edgar-lakehouse/chat/anthropic_api_key` (Secrets Manager, placeholder until
  the human fills it — see SETUP-CREDENTIALS.md), and it is optional: Bedrock
  needs no key at all.
- **SEC5** Cloud SG (when enabled): ingress from ALB SG only, egress 443 only.
- **SEC6** LiteLLM telemetry disabled at import on the ADK path; CI grep
  forbids `litellm.success_callback`.
- **SEC7** Logs to stdout locally / CloudWatch in cloud (14-day retention).
- **SEC8** CI grep gates: `databricks`, `vertexai`, `google-cloud-`,
  f-string SQL, `st.secrets`, `litellm.success_callback` — all zero. Plus the
  repo-standard `scripts/secret-scan.sh`.

## 8. Infrastructure (repo 2 — this repo's footprint)

Added to repo 2 as `modules/chatbot`, applied via PR (explicitly authorized to
cross the repo boundary for this build):

**Applied now (no recurring cost):**
- SSM: `/edgar-lakehouse/chat/enabled` = `true`, `/edgar-lakehouse/chat/model/main`
  and `/model/cheap` (empty default = "probe"), `/edgar-lakehouse/chat/token_budget_day`.
- Secrets Manager: `/edgar-lakehouse/chat/anthropic_api_key` **placeholder**
  (value `PLACEHOLDER-set-me`); Terraform creates the secret container only —
  the value is set by hand so it never enters state once real.

**Written but flagged off (`deploy_chatbot=false`):** ECS Fargate service
(0.5 vCPU/1 GB, `desired_count=1`), task role (`bedrock:InvokeModel` +
`bedrock:Converse` on configured models, `s3:GetObject` on
`edgar-lake-serving/v1/*`, the chat SSM path, the one secret), log group.
Reason in-code: always-on Fargate+ALB ≈ $30+/month against the project's $10
alarm; the demo is local-first. Flipping the flag is the entire deploy.

## 9. Config resolution

`env var → SSM → default-or-fail naming the key` (project rule 3).

```
AGENT_IMPL=langgraph|adk        DEPLOY_ENV=local|cloud
DATA_DIR=./data                 SERVING_PREFIX=            # s3://… when repo 4 exports
BEDROCK_MODEL_ID=               # pin; empty = probe candidates
BEDROCK_MODEL_CHEAP=            # topic gate; empty = same as main
TOKEN_BUDGET_DAY=200000         TOPIC_GATE=on
ANTHROPIC_API_KEY=              # optional direct-API fallback; normally unset
```

Model candidates, quality-ordered, first invocable wins (probe = one 5-token
converse): Claude Haiku 4.5 → Claude Sonnet 4.5 → Nova Pro → Nova Lite. The
day the Anthropic use-case form is approved, the app upgrades itself with no
change.

## 10. Repository layout (target)

```
├── AGENTS.md                  # rewritten against this doc
├── app.py                     # thin streamlit entry
├── run.bat                    # local launcher (SSO refresh + streamlit)
├── pyproject.toml             # pinned deps incl. dev extra
├── scripts/
│   ├── export_gold.py         # Databricks gold -> data/*.parquet (+manifest)
│   ├── check.ps1 / check.sh   # ruff + mypy + pytest, same as CI
│   └── secret-scan.sh
├── src/finchat/
│   ├── config.py              # env→SSM→fail; model probing
│   ├── prompts.py             # system prompt + ALL fixed texts
│   ├── guard/limits.py        # buckets, budget, kill switch
│   ├── data/store.py          # DuckDB read-only, local/S3, TTL refresh
│   ├── tools/impl.py          # the 9 tools
│   ├── tools/registry.py      # names→fn + JSON schemas (single source)
│   ├── agent/base.py          # AgentRunner protocol + TurnResult
│   ├── agent/lg_runner.py     # LangGraph
│   ├── agent/adk_runner.py    # ADK (optional import)
│   └── ui/chat.py             # streamlit page, trace expander
├── tests/                     # see 21-test-plan.md
└── .github/workflows/ci.yml
```

## 11. Validation rules (each one is a test — see 21-test-plan.md)

- **V1** Tool params reject: bad cik, over-limit, unknown concept, q too short.
- **V2** `LIMIT` in SQL comes from the server constant even when the model asks
  for more.
- **V3** Envelope always complete; `truncated` set exactly when capped.
- **V4** Fixed texts byte-identical across impls and never model-generated.
- **V5** Loop halts: 4th round → TOO_BROAD; 8k tool tokens → TOO_BROAD.
- **V6** Guard: 11th msg in a minute → LIMIT_TEXT; 61st in a day → LIMIT_TEXT;
  budget exhaustion → BUDGET_TEXT; kill switch → KILLED_TEXT; UTC reset works.
- **V7** Store: INSERT/ATTACH/COPY raise; refresh is single-flight; missing
  data dir → actionable error naming `export_gold.py`.
- **V8** Concept enum derives from data; a concept present in data is always
  accepted.
- **V9** Advice questions produce refusal with figures offered; no tool runs.
- **V10** Unknown company → resolves to "not in dataset" listing the 8, never a
  hallucinated figure.
- **V11** `import finchat.*` never imports `databricks`, `streamlit` outside
  `ui/`, or opens network at import time.
- **V12** Config: missing required key fails naming it; SSM unreachable
  locally → documented defaults, cloud → hard fail.
```
