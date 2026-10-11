# Rules for AI coding agents working on pr-triage

pr-triage reviews pull requests for other repos, so a mistake here can wave a bad change through.
Keep it boring and predictable.

## Non-negotiables

- The model never decides. Only `decide.py` turns evidence into a result, and no path may lead from
  a model answer alone to "approvable". Add a test for every new path into `decide()`.
- Only the review Lambda may call Bedrock, and only the configured model. Do not widen that policy.
- PR content, workflow artifacts and model answers are untrusted. Never use a URL or a host from
  them; preview URLs come from our CloudFormation outputs and our own domain.
- Never print, log or store GitHub App credentials or webhook secrets outside SSM SecureString.
- Every limit or failure ends in "needs human review", never in silence and never in approval.

## How to work

- Keep the shared names and formats in `docs/contracts.md` in step with the demo repos. Change the
  contract first, then every repo that depends on it.
- `policy/policy.yaml` and `prompts/` change review behavior. A change to either needs a short note
  in the PR on what it changes for a reviewed PR.
- One purpose per PR. Run before pushing:
  - `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/python -m pytest`
  - `cd infra && npx tsc && npm test && npx cdk synth`
- Pure logic lives outside `handlers/` so it can be tested without AWS. Handlers stay thin.
- Do not add dependencies to `requirements.txt` without a reason in the PR; every Lambda carries them.
