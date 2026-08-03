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
| `test_store.py` | V7, SEC1, G6 | tables MATERIALIZED then hardened; queries succeed AFTER hardening; INSERT/ATTACH/COPY/`LOAD httpfs` raise; `current_setting('enable_external_access')` = false; manifest parsed; single-flight refresh (two threads, one reload); missing data dir error names export_gold.py; S3 fetch is an injected callable (stubbed — untested against real S3 until repo 4 ships) |
| `test_tools.py` | V1, V2, V3, V8, G1 | per tool: happy path on fixture Parquet; param rejections; server-side LIMIT wins; envelope complete; truncation flag; citations present where specified; concept enum derived from fixture data |
| `test_prompts.py` | V4, G9 | fixed texts (incl. ERROR_TEXT) are constants; byte-identical across impls; system prompt contains the tool-data-is-not-instructions clause; `redact()` strips ARNs/12-digit ids/s3 URIs/abs paths/dapi/AKIA from a synthetic AccessDenied string; `sanitize_markdown()` strips images and non-sec.gov links; tool-data block contains exactly one closing nonce delimiter |
| `test_guard.py` | V6, G5 | bucket math at the boundary (10th ok, 11th refused); day counter UTC reset (frozen time); budget RESERVED before call, settled after, survives simulated restart (file-backed); kill switch cached 30 s; fail-open ONLY when DEPLOY_ENV=local, unset env fails closed |
| `test_loop_lg.py` (`loop`) | G1–G4, V5, V9, V10 | scripted turns: happy path with 2 tool calls → answered + trace matches script; OUT_OF_SCOPE token → REFUSAL_TEXT; 4th round → TOO_BROAD_TEXT; char cap → TOO_BROAD_TEXT; tool exception → outcome=error + ERROR_TEXT (no exception text in output); gate error → fail-open with gate_error in trace and no extra main tokens |
| `test_loop_adk.py` (`loop`) | G2, G9 | same script through the ADK runner; sequential turns share a session; two sessions do not cross-contaminate; **skip with reason** when google-adk absent |
| `test_structural.py` | SEC2, SEC6, SEC8, V11 | grep gates as tests: no f-string SELECT in tools/; no `databricks`/`vertexai`/`st.secrets`/`httpfs` in src/; adk module sets `litellm.telemetry = False` and `LITELLM_LOCAL_MODEL_COST_MAP`; `finchat.tools`+`finchat.data` import with sockets monkeypatched to raise (no network at import) |
| `test_ui_contract.py` | G1 | trace renderer formats a TurnResult without touching agent internals (UI depends only on the protocol) |
| `test_live.py` (`live`) | G7, G1, SEC3 | one in-scope question: numeric-provenance check (every number token in the answer appears in some tool result or the allowlist: years/ordinals/counts); one refusal; one injection canary (poisoned fixture company + same payload as query → canary string absent, no out-of-registry tool) |

## Coverage

`pytest --cov=src/finchat --cov-fail-under=85`, non-`live` suites only.
Excluded from the floor with reasons stated in pyproject: `ui/chat.py`
(Streamlit script body — logic lives in testable helpers) and
`agent/adk_runner.py` when google-adk is absent (covered by the CI adk job
instead). mypy gets a `[[tool.mypy.overrides]]` ignoring missing imports for
`google.adk.*`/`litellm` so `--strict` passes without the optional extra.

## The check gate

`scripts/check.ps1` (and `.sh`, and CI) run identically:

```powershell
# scripts/check.ps1 (the .sh rendering differs only in env-var syntax)
ruff check .; ruff format --check .
mypy --strict src/
pytest -m "not live" --cov=src/finchat --cov-fail-under=85
$env:AGENT_IMPL='adk'; pytest -m loop; Remove-Item Env:AGENT_IMPL
bash scripts/secret-scan.sh
```

CI additionally runs a dedicated `adk` job: install the `[adk]` extra, run
`pytest -m loop` with **skips promoted to failures** — the seam cannot go
green unexecuted.

Definition of done for the build loop: the check gate green, every G and V row
above implemented, `pytest -m live` output pasted once into PROGRESS.md
(**redacted — PROGRESS.md is public**), and the local acceptance script in
`SETUP-CREDENTIALS.md` walked once.
