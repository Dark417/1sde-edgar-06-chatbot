# SETUP — credentials, keys, and the local acceptance script

Everything a human must do, in order. Nothing here is optional guesswork: each
step says what it unlocks and how to verify it worked.

## 0. What is ALREADY set up (nothing to do)

| Thing | State |
|---|---|
| AWS SSO profile `edgar-sso` | working; `aws sso login --profile edgar-sso`, or double-click `D:\aws-login.bat` |
| SSM config `/edgar-lakehouse/chat/*` | applied by repo 2 (`enabled=true`, `model/main=probe`, `model/cheap=probe`, `token_budget_day=200000`) |
| Secret container `/edgar-lakehouse/chat/anthropic_api_key` | exists, holds `PLACEHOLDER-set-me` |
| Amazon Nova (Bedrock) | invocable today — the app runs on it with zero further setup |

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
.venv\Scripts\python scripts\export_gold.py
```

## 2. 🔴 Unlock Claude on Bedrock (the "API key" for the main path)

Bedrock needs **no API key** — auth is your AWS identity. What it needs is
one-time **model access** plus, for Anthropic models, a **use-case form**
(this account currently gets `ResourceNotFoundException: Model use case
details have not been submitted` on every Claude model — verified).

1. Sign in (aws-login.bat) → Bedrock console, region **us-east-2**:
   `https://us-east-2.console.aws.amazon.com/bedrock/home?region=us-east-2#/modelaccess`
2. **Model access → Modify model access** → tick the Anthropic Claude models
   (Haiku 4.5 and Sonnet 4.5 at minimum) → Next.
3. The **Anthropic use-case form** appears inline: company/use-case questions.
   Fill honestly: personal portfolio project, financial-data Q&A over public
   SEC filings, no end users' PII. Submit.
4. Access usually flips to "Access granted" within minutes; retry after ~15 if
   not.
5. Verify:
   ```powershell
   aws bedrock-runtime converse --model-id "us.anthropic.claude-haiku-4-5-20251001-v1:0" `
     --messages '[{\"role\":\"user\",\"content\":[{\"text\":\"say OK\"}]}]' `
     --inference-config '{\"maxTokens\":5}' --profile edgar-sso --region us-east-2
   ```
6. **No code change needed**: the app probes Claude first and upgrades itself
   on the next start. To pin instead of probe:
   ```powershell
   aws ssm put-parameter --name /edgar-lakehouse/chat/model/main `
     --value "us.anthropic.claude-haiku-4-5-20251001-v1:0" --overwrite `
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
