# SETUP — credentials, keys, and the local acceptance script

Everything a human must do, in order. Nothing here is optional guesswork: each
step says what it unlocks and how to verify it worked.

## 0. What is ALREADY set up (nothing to do)

| Thing | State |
|---|---|
| AWS SSO profile `edgar-sso` | working; `aws sso login --profile edgar-sso`, or double-click `D:\aws-login.bat` |
| SSM config `/edgar-lakehouse/chat/*` | applied by repo 2 v0.4.0 (`enabled=true`, `model/main=`Sonnet 4.5, `model/cheap=`Haiku 4.5, `token_budget_day=200000`) |
| Secret container `/edgar-lakehouse/chat/anthropic_api_key` | exists, holds `PLACEHOLDER-set-me` |
| Claude on Bedrock | Sonnet 4.5 / Haiku 4.5 / Opus 4.5 all invocable; Nova stays as the fallback |
| CI OIDC role `1sde-edgar-06-chatbot-ci` | created by repo 2 v0.4.0, with a scoped `bedrock:InvokeModel` policy |

## 1. Run it locally (works right now)

```powershell
cd D:\1sde\0databricks\1sde-edgar-06-chatbot
run.bat        # refreshes SSO if needed, sets DEPLOY_ENV=local, starts Streamlit
```

If `data\` is empty first run the export (needs the Databricks PAT from repo
1's gitignored `changelog/liquibase.properties`):

```powershell
$env:DBX_HOST = "<workspace host>"          # docs/LOCAL-VALUES.md
$env:DBX_WAREHOUSE_ID = "<warehouse id>"    # docs/LOCAL-VALUES.md
$env:DBX_TOKEN = "<pat>"
aws s3 sync s3://<serving-bucket>/v1 ./data   # repo 4 owns this export
```

## 2. ✅ Claude on Bedrock — DONE for this account, nothing to do

Bedrock needs **no API key**: auth is your AWS identity, so the `edgar-sso`
profile is the credential. Anthropic models additionally require a one-time
**use-case form** per account.

**That form was submitted and approved on 2026-08-02.** Verified invocable in
the project account, us-east-2: Claude **Sonnet 4.5**, **Haiku 4.5** and
**Opus 4.5**. The model is now pinned to Sonnet 4.5 in SSM by repo 2, so the
app no longer probes on startup. There is nothing for you to click.

<details><summary>If you ever set this up in a fresh AWS account</summary>

The old **Model access** page is retired — serverless models now enable
themselves on first invocation. The Anthropic gate lives on the **Model
catalog** page instead:

1. Bedrock console, region **us-east-2** → **Model catalog**.
2. Banner at the top: **"Submit use case details"**.
3. Fill it honestly — company/website, industry, intended users, and a
   description of the use case. For this project: an individual developer's
   portfolio demo doing financial-data Q&A over public SEC filings, low
   volume, no PII.
4. Approval took under 15 minutes. Until it lands, every Claude model returns
   `ResourceNotFoundException: Model use case details have not been submitted`,
   and the app silently falls back to Amazon Nova (which needs no form).
</details>

Verify at any time:
```powershell
aws bedrock-runtime converse --model-id "us.anthropic.claude-sonnet-4-5-20250929-v1:0" `
  --messages '[{\"role\":\"user\",\"content\":[{\"text\":\"say OK\"}]}]' `
  --inference-config '{\"maxTokens\":5}' --profile edgar-sso --region us-east-2
```

To pin a model instead of probing (skips one round trip at startup):
```powershell
aws ssm put-parameter --name /edgar-lakehouse/chat/model/main `
  --value "us.anthropic.claude-sonnet-4-5-20250929-v1:0" --overwrite `
  --profile edgar-sso --region us-east-2
```

## 3. Optional — direct Anthropic API key (fallback path, placeholder today)

Only needed if you ever want the app to call Anthropic's API directly instead
of Bedrock. Get a key at `https://console.anthropic.com/settings/keys`, then:

```powershell
aws secretsmanager put-secret-value `
  --secret-id /edgar-lakehouse/chat/anthropic_api_key `
  --secret-string "sk-ant-..." --profile edgar-sso --region us-east-2
```

Terraform never touches the value again (`ignore_changes`; no
secret_version resource exists — repo 2's policy gate forbids it). Leave it as
`PLACEHOLDER-set-me` until needed; the app treats the placeholder as "not
configured" and stays on Bedrock.

## 4. The kill switch and budget (operate the demo)

```powershell
# turn the assistant off for everyone (takes effect within 30 s):
aws ssm put-parameter --name /edgar-lakehouse/chat/enabled --value false --overwrite --profile edgar-sso --region us-east-2
# back on:
aws ssm put-parameter --name /edgar-lakehouse/chat/enabled --value true --overwrite --profile edgar-sso --region us-east-2
# raise/lower the daily token ceiling:
aws ssm put-parameter --name /edgar-lakehouse/chat/token_budget_day --value 500000 --overwrite --profile edgar-sso --region us-east-2
```

## 5. Local acceptance script (the G7 walk — 5 minutes)

With `run.bat` running at `http://localhost:8501`:

- [ ] Sidebar shows dataset counts and the probed model id
- [ ] "What companies do you have data on?" → the 8 companies, with a tool
      trace under the answer
- [ ] "Which company had the biggest material restatement?" → names one, with
      original/restated values and the heuristic disclaimer
- [ ] "Should I buy Apple stock?" → refusal offering figures; no tools in trace
- [ ] "What was Tesla's revenue?" → says not in the dataset, lists the 8
- [ ] 11 rapid messages → the 11th gets the rate-limit text
- [ ] Flip the kill switch off (step 4) → next message gets the switched-off
      text within 30 s → flip back on
- [ ] Toggle the sidebar to `adk` (if the extra is installed) → a question
      still answers, trace still renders

## 6. Cloud deploy (when wanted, not now)

Repo 2 `modules/chatbot` carries the config; compute is intentionally absent
(always-on Fargate+ALB ≈ $30+/month vs the $10 alarm). The path: add the ECS
service/task/SG behind `deploy_chatbot=true` with `desired_count=1` and
`deployment_maximum_percent=100`, task role scoped to `bedrock:InvokeModel`/
`Converse` on the configured models + `s3:GetObject` on
`edgar-lake-serving/v1/*` + the chat SSM path + the one secret; set
`DEPLOY_ENV=cloud` in the task definition (fail-closed).
