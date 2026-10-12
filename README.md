# pr-triage

AI coding agents made pull requests fast. Checking them did not get faster: a person still has to
read the code, run it, and work out whether it does what the ticket asked. pr-triage gives every PR a
first review the moment it opens, so a person only spends time where it is needed.

Every PR ends in one of three results, bound to the commit that was reviewed:

| Result | Meaning | Check run |
|---|---|---|
| **Approvable** | Every check passed and the AI review found nothing to fix. A person approves, or in auto-approve mode pr-triage approves and merges it into `dev` when it can vouch for it. | `success` |
| **Changes requested** | The author has something concrete to fix first. | `failure` (blocks merge) |
| **Needs human review** | Security-sensitive, suspicious, or something the system cannot judge. | `neutral` |

## How it decides

```
GitHub ─webhook─▶ API Gateway ─▶ ingest Lambda ─▶ SQS ─▶ dispatcher ─▶ Step Functions (one run per commit)
                  signature check, duplicate check, answer within 10 s

 1 Collect context     PR, diff, ticket from the default branch, other PRs of the same ticket  → S3
 2 Check policy        ticket, expected behavior, sensitive paths, size, injection tripwire    (code)
 3 Wait for CI         task token; resumed by the workflow_run webhook, 1 hour limit
 4 Check results       CI, test relevance and expected behavior in CI (v2), on an AWS preview (v3)
 5 AI review           Claude on Amazon Bedrock, one forced tool call, within daily and monthly limits
 6 Validate answer     files, lines, endpoints and log groups the AI cites must exist     (code)
 7 Decide              the rules below                                                   (code)
 8 Publish             check run on the commit; label and comment only if it is still the PR head
```

- **The AI suggests, code decides.** The model can push a PR toward "changes requested" or "needs
  human review", but never to "approvable" on its own: every cheaper gate has to pass too. The review
  Lambda can call one Bedrock model and nothing else.
- **Cheap checks first.** A PR that fails policy or CI never reaches the model.
- **Results belong to a commit.** A new commit starts a new review and stops the old one.
- **Requirements come from the default branch.** A PR cannot change the ticket it is judged against.
- **Failure is visible.** If any step fails, the PR gets "needs human review" instead of hanging.

Decision rules (`src/pr_triage/decide.py`): a security-related finding (sensitive path, prompt
injection) means a person reviews; otherwise anything the author can fix comes first; any other
doubt goes to a person; only a clean run is approvable.

Advisory mode, the default, never approves: a person approves and merges. Auto-approve mode
(`/pr-triage/config/mode`) approves an approvable PR into `dev` and merges it, with no person,
only when code can vouch for it (`decide.auto_blocker`):

- docs-only and tests-only PRs;
- code PRs whose expected behavior actually ran and passed, in CI from v2 and on the AWS preview
  at v3, whose new tests fail on the old code, whose ticket lives in this repo alone, and which
  have no UI items for a person to look at.

The merge names the reviewed commit, so GitHub refuses it if a newer commit arrived. Everything
else waits for a person, and promotion beyond `dev` stays with people. The GitHub App needs
contents write access for this; its approval counts as the one required review.

## Levels

| Level | Adds | Where PR code runs |
|---|---|---|
| v1 | policy, CI result, AI reads the code against the ticket and the expected behavior | nowhere |
| v2 | test relevance (new tests must fail on the old code), expected behavior run in CI, read-only check on dev after merge | GitHub Actions |
| v3 | the ticket's PRs deployed together to an AWS preview, expected behavior run there | a preview stack per ticket |

`scripts/set_level.sh v1|v2|v3` switches pr-triage and the demo repos together.

## Isolating previews in one AWS account

Previews run in the same account as pr-triage, so three layers keep them contained
(`infra/lib/preview-platform-stack.ts`):

1. GitHub Actions roles trust one repo and one event type each, may only touch `inv-api-*` stacks,
   and may only pass one CloudFormation role.
2. That CloudFormation role can create only what the demo app uses, under `inv-api-*` names, and
   cannot create an IAM role without the permissions boundary.
3. The boundary caps every app role at its own table and its own logs.

Fork PRs get no OIDC token, no preview and no AI call unless a maintainer labels the commit
`ai-review-ok`. Previews are removed when the PR closes and, as a backstop, after 24 hours.
For a real organization, a separate preview account is still the stronger choice.

## Cost

The model is Claude Haiku 4.5 through the Australian inference profile, so requests stay in
Australian regions; the model id and its price per million tokens are settings in `infra/cdk.json`.
The app counts the cost of every call from the token usage
Bedrock returns, reserves the worst case before calling, and stops at a daily review count and a
monthly AI budget (both in SSM). AWS Budgets is the backstop: it warns at $5 and switches AI reviews
off at $8. Hitting any limit sends the PR to a person; it never turns into an approval. Everything
else is serverless and costs close to nothing while idle.

## Repository

```
src/pr_triage/   Lambda code (Python 3.13), one module per step in handlers/
policy/          policy.yaml: sensitive paths, sizes, required workflows per level
prompts/         system prompt and the submit_review tool schema
infra/           CDK (TypeScript): PreviewPlatform and Core stacks
scripts/         GitHub App setup, demo repo setup, level switch, cycle report
docs/            contracts.md: names and formats shared with the demo repos
tests/           pytest
```

The demo app lives in [demo-inventory-api](https://github.com/jaeneungsim/demo-inventory-api) and
[demo-inventory-web](https://github.com/jaeneungsim/demo-inventory-web). Tickets are files in
`demo-inventory-api/tickets/`; the part that reads them is the only piece to replace for Jira.

## Running it

```bash
uv venv --python 3.13 .venv && uv pip install -r requirements-dev.txt
.venv/bin/python -m pytest && .venv/bin/ruff check .
cd infra && npm ci && npm test && npx cdk synth
```

Deploying needs an AWS account with Bedrock access to the model in `infra/cdk.json`:

1. `npx cdk deploy --all` (set `githubOidcProviderArn` in `cdk.json` if the account already has a
   GitHub OIDC provider, and `alertEmail` for budget emails).
2. `python3 scripts/register_github_app.py open --webhook-url <WebhookUrl output>`: paste the secret
   from the clipboard, create the app and generate a private key. Then
   `python3 scripts/register_github_app.py store --app-id <id> --pem <file>` and install the app on
   the two demo repos.
3. `scripts/configure_demo_repos.sh` to set repo variables, labels and branch protection.

This project was built with an AI coding agent, following the rules in `AGENTS.md`.
