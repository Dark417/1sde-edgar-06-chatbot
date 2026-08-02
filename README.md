# 1sde-edgar-06-chatbot

**Repo 6 of 6** in the edgar-lakehouse project — a natural-language interface over
the gold marts. Ask "which companies restated revenue most materially this year?"
and get an answer with the accession numbers it came from.

```
1 contracts ─► 2 infra ─► 3 ingest ─► 4 pipelines ─┬─► 5 serving  (REST + UI)
                                                    └─► 6 chatbot (NL + tools)
```

## The one rule

**The model never computes a number.** It chooses which tool to call; the tool
runs fixed SQL over the gold Parquet and returns data plus citations. Sums,
rankings, deltas and date filtering happen in SQL, never in the LLM. Every figure
in an answer carries the asserting `accession_number` and a sec.gov link.

That is the difference between a demo that impresses and one a finance team could
trust — a confidently wrong revenue figure is worse than no chatbot.

## Design

- **Tool calling** — 8 typed, read-only functions over gold; no side effects.
- **Context engineering** — `schema_card.md`, `metrics.yaml`, `examples.yaml`,
  `policy.md` as versioned assets with tests, not inline strings.
- **Routing** — company / cross-company / metadata / out-of-scope /
  advice-seeking, with advice structurally unable to reach the tool executor.
- **Memory** — bounded conversation window plus resolved-entity slots, so
  "what about 2023?" works. No long-term or vector memory.
- **Guardrails** — citation verification, no investment advice, injection screen,
  hard caps that are *reported* rather than silently applied.
- **Orchestration** — LangGraph for explicit, testable control flow; LangChain
  only for model/tool adapters; Bedrock for inference.

**No RAG and no vector store**, deliberately: the data is structured and numeric,
so retrieval is a `WHERE` clause. **No Databricks connection**, deliberately:
Free Edition compute can shut down for the day and the demo must survive it.

Full rationale: [docs/10-agent-design.md](docs/10-agent-design.md) ·
Build brief: [AGENTS.md](AGENTS.md)

## Status

Design complete; implementation not started. See `AGENTS.md` §6 for the feature
list and §11 for the definition of done.

> Built on Databricks **Free Edition**, which is not licensed for commercial use —
> this is a portfolio/demo project. The bot answers from a daily batch export and
> discloses its own staleness; it does not give investment advice.
