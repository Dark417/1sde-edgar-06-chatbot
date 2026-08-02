# 1sde-edgar-06-chatbot

Chat with the EDGAR lakehouse gold layer in plain English. Streamlit UI, Amazon
Bedrock for language, DuckDB over exported Parquet for every number.

## The one rule

**The model never calculates.** It chooses a tool; the tool runs fixed SQL and
returns rows. Sums, rankings, deltas and date filtering all happen in SQL. Every
answer carries an expandable "How I got this" panel showing the exact tool calls
and arguments used — so a wrong answer is diagnosable rather than mysterious.

## Run it

```bash
python -m venv .venv && .venv/Scripts/pip install streamlit duckdb boto3 pyarrow

# one-time: pull gold out of Databricks into local Parquet
set DBX_HOST=<workspace-host>
set DBX_WAREHOUSE_ID=<warehouse-id>
set DBX_TOKEN=<pat>
.venv/Scripts/python scripts/export_gold.py

run.bat            # or: streamlit run app.py
```

`run.bat` refreshes the AWS SSO session if needed and starts the UI on
http://localhost:8501.

## What you can ask

- "What companies do you have data on?"
- "Which company had the biggest material restatement?"
- "Compare revenue across all companies"
- "Tell me about Apple's profile and financials"
- "How many restatements are there, and which concepts get restated most?"

## Design notes

- **No live Databricks dependency.** The app reads Parquet exported from gold,
  so a Free Edition quota shutdown cannot take it down.
- **Model is probed, not assumed.** It tries Claude first and falls back to
  Amazon Nova, so it upgrades itself when Anthropic access is enabled.
- **Refuses investment advice** and says precisely what the dataset lacks
  rather than guessing.
- `materiality_band` is a product heuristic, not an accounting standard, and is
  labelled as such everywhere it appears.

Full rationale: [docs/10-agent-design.md](docs/10-agent-design.md) ·
Build brief: [AGENTS.md](AGENTS.md)

> Portfolio project on Databricks Free Edition, not licensed for commercial use.
> Not investment advice.
