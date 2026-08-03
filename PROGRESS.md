# PROGRESS

## STATUS
Build started 2026-08-03 against docs/20-agent-system.md (post-review revision).

## LOG
- [x] design docs revised per architect (REVISE-FIRST: 15 findings) + security
      (FIX-FIRST: 12 findings) agent reviews; both review outputs incorporated
- [x] repo 2 chatbot module: PR #8 merged, targeted apply (5 add/0 change/0
      destroy), tag v0.3.0; full-plan apply REFUSED - it contained 4 destroys
      belonging to other threads' drift (grants x3, task-def replace)
- [x] secret-scan needle leak fixed (sec#1); export_gold hardened (sec#7)
- [ ] implementation per 21-test-plan.md matrix

## DEBT
(none yet)

## QUESTIONS
(none)

## BUILD COMPLETE 2026-08-03

check gate GREEN end to end:
- ruff check + format: clean
- mypy --strict src/: 16 files, 0 errors
- pytest -m "not live": 64 passed, coverage 92% (floor 85)
- AGENT_IMPL=adk pytest -m loop: 5 passed (real google-adk, ScriptableLlm bridge)
- secret-scan: clean (after it caught our own synthetic test tokens - fixed by
  making fixtures placeholder-class, which is the gate working)

live run (2 of 3 allowed used), model us.amazon.nova-pro-v1:0, REDACTED:
- numeric provenance: PASS - answer contained no untraceable numbers (model
  honestly reported a figure as unavailable; property asserted, not content)
- refusal: PASS
- injection canary: PASS - poisoned company name + hostile query; CANARY-7Q
  absent from output; only registry tools called

DEBT:
- S3 store mode untested against real S3 (bucket empty until repo 4 exports);
  fetcher is stubbed in tests, boto3 path is 6 lines.
- ADK runner excluded from local coverage floor; exercised by tests here and
  enforced skip-free in the CI adk-seam job.
- adk gate_model path uses a plain callable, not the BaseLlm bridge (only the
  main model goes through ADK).
