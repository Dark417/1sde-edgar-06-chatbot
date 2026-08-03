# 21 — Test plan

> Maps every target (G) and validation rule (V) from `20-agent-system.md` to a
> concrete test. A target with no test row here is a target we do not have.

## Markers

| marker | meaning | needs network? | in CI |
|---|---|---|---|
| *(none)* | pure unit | no | yes |
| `loop` | full turn against a **scripted fake model** | no | yes, under both `AGENT_IMPL` values (adk skips-with-reason if not installed) |
| `live` | one real Bedrock round | yes | no — run manually, ≤3 times, output pasted into PROGRESS.md |

The fake model is a scripted `BaseChatModel` (and the ADK equivalent) that
emits a predetermined sequence of tool calls then a final answer. It is how
G2/G3/G4 are testable deterministically and for free.

## Matrix

| Test file | Covers | Asserts |
|---|---|---|
| `test_config.py` | V12 | missing key names itself; env beats SSM; local defaults vs cloud hard-fail; model pin bypasses probe |
| `test_store.py` | V7, SEC1, G6 | INSERT/ATTACH/COPY raise; views registered; manifest parsed; single-flight refresh (two threads, one reload); missing data dir error names export_gold.py |
| `test_tools.py` | V1, V2, V3, V8, G1 | per tool: happy path on fixture Parquet; param rejections; server-side LIMIT wins; envelope complete; truncation flag; citations present where specified; concept enum derived from fixture data |
| `test_prompts.py` | V4, G9 | fixed texts are constants; byte-identical across impl modules; system prompt contains the tool-data-is-not-instructions clause |
| `test_guard.py` | V6, G5 | bucket math at the boundary (10th ok, 11th refused); day counter UTC reset (frozen time); budget debit and exhaustion; kill switch cached 30 s; fail-open local / fail-closed cloud |
| `test_loop_lg.py` (`loop`) | G1–G4, V5, V9, V10 | scripted turns: happy path with 2 tool calls → answered + trace matches script; OUT_OF_SCOPE token → REFUSAL_TEXT; 4th round → TOO_BROAD_TEXT; 8k tool tokens → TOO_BROAD_TEXT; advice question → refusal, zero tools; unknown company → lists the 8 |
| `test_loop_adk.py` (`loop`) | G2, G9 | same script through the ADK runner; sequential turns share a session; two sessions do not cross-contaminate; **skip with reason** when google-adk absent |
| `test_structural.py` | SEC2, SEC6, SEC8, V11 | grep gates as tests: no f-string SELECT in tools/; no `databricks`/`vertexai`/`google-cloud-`/`st.secrets`/`litellm.success_callback` in src/; `finchat.tools`+`finchat.data` import without streamlit and without network |
| `test_ui_contract.py` | G1 | trace renderer formats a TurnResult without touching agent internals (UI depends only on the protocol) |
| `test_live.py` (`live`) | G7 sanity | one in-scope question (answer contains a number + tool trace) and one refusal, against the probed real model |

## Coverage

`pytest --cov=src/finchat --cov-fail-under=85`, non-`live` suites only.
`ui/chat.py` is excluded from the floor (Streamlit script body); its logic
lives in testable helpers.

## The check gate

`scripts/check.ps1` (and `.sh`, and CI) run identically:

```
ruff check . && ruff format --check .
mypy --strict src/
pytest -m "not live" --cov=src/finchat --cov-fail-under=85
AGENT_IMPL=adk pytest -m loop      # skips cleanly when adk not installed
bash scripts/secret-scan.sh
```

Definition of done for the build loop: the check gate green, every G and V row
above implemented, `pytest -m live` output pasted once into PROGRESS.md, and
the §9 manual script from AGENTS.md walked once locally.
