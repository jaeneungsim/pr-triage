# Interface contract

This file fixes the names and formats that `pr-triage`, `demo-inventory-api` and
`demo-inventory-web` share. Change it here first, then change the code in every
repo that depends on it.

## 1. Identifiers

| Item | Value |
|---|---|
| GitHub owner | `jaeneungsim` (CDK context `githubOwner`) |
| Repos | `pr-triage`, `demo-inventory-api`, `demo-inventory-web` |
| Default branch of the demo repos | `dev` (the development branch; production is out of scope) |
| Default branch of pr-triage | `main` |
| AWS region | `ap-southeast-2` |
| Ticket key | `INV-<digits>`, regex `\bINV-(\d+)\b` |
| Stage name | the ticket key in lower case, for example `inv-123`. The dev environment uses `dev` |

**Where the ticket key comes from:** the head branch name first (`INV-123-stock-in-detail`),
then the PR title (`INV-123 Add stock to the item response`). Commit messages must
contain the key too (`INV-123: add stock to the item response`); the commit-msg hook
enforces this.

## 2. Ticket files

Tickets live only in `demo-inventory-api/tickets/<KEY>.md`. Reviews always read them
from the default branch (`dev`), so a PR cannot change its own requirements.

```markdown
---
key: INV-123
title: Show the stock quantity on the item detail page
status: in-progress
repos: [demo-inventory-api, demo-inventory-web]
---
## Requirements
- R1: The item API response includes the stock quantity (`stock`).
- R2: The item detail page shows the stock quantity.
```

- Front matter is YAML between the first two `---` lines.
- Requirements are lines `- R<n>: <text>` under `## Requirements`.

## 3. PR description

Both demo repos ship `.github/pull_request_template.md` with these sections:

````markdown
## Ticket
INV-123

## Type
<!-- one of: feature, bugfix, refactor, tests, docs -->
feature

## Summary
One or two sentences.

## Expected behavior
```yaml expected-behavior
- id: B1
  covers: [R1]
  type: api
  request: GET /items/42
  expect:
    status: 200
    body_has: [stock]
- id: B2
  covers: [R1]
  type: log
  after: B1
  expect:
    no_level: ERROR
- id: B3
  covers: [R2]
  type: ui
  page: /item.html?id=42
  step: The detail page for item 42 shows the stock quantity.
```
````

The expected behavior is a fenced block in the PR description whose info string is
exactly `yaml expected-behavior`. It is not a file. The block holds a YAML list.

### Expected-behavior items

| Field | Applies to | Meaning |
|---|---|---|
| `id` | all | `B<n>`, unique in the block |
| `covers` | all | list of requirement ids from the ticket (`R1`) |
| `type` | all | `api`, `log` or `ui` |
| `request` | api | `"<METHOD> <path>"`. METHOD is GET, POST, PUT, PATCH or DELETE. The path starts with `/` and has no host |
| `headers` | api | optional map of request headers |
| `body` | api | optional JSON object sent as the request body |
| `expect.status` | api | expected HTTP status (int) |
| `expect.body_has` | api | optional list of keys that must exist. Dotted paths (`item.stock`) are allowed |
| `expect.body_not_has` | api | optional list of keys that must not exist |
| `expect.body_includes` | api | optional object. Every key and value must appear in the response body (deep subset match) |
| `after` | log | id of the api item whose request the log check looks at |
| `expect.no_level` | log | `ERROR`: no log line at ERROR or above for that request |
| `expect.has_message` | log | optional substring that must appear in a log line for that request |
| `step` | ui | what a person should check |
| `page` | ui | optional page path on the web preview |

Only api items with method GET are used for the read-only post-merge check on dev.

## 4. Demo API (`demo-inventory-api`)

### Behavior on `dev` before any demo ticket

| Route | Response |
|---|---|
| `GET /health` | `200 {"ok": true}` |
| `GET /items` | `200 {"items": [{"id", "name", "price"}, ...]}` |
| `GET /items/{id}` | `200 {"id", "name", "price"}` or `404 {"error": "not_found"}` |
| `POST /items` | `201` with the created item, `400 {"error": "...", "detail": "..."}` on invalid input |

- Seed data (every environment): item `41` (stock 12), `42` (stock 5), `43` (stock 0).
  The table stores `stock`, but the API does not return it yet. That is ticket INV-123.
- Every response has the header `x-request-id`.
- Logs are JSON lines on stdout: `{"level", "request_id", "message", "route", "status"}`.
- One Lambda function routes every request (HTTP API `$default` route).

### Stack

| Item | Value |
|---|---|
| Stack name | `inv-api-preview-<stage>` for tickets, `inv-api-dev` for dev |
| Function name | `<stack>-handler` |
| Log group | `/aws/lambda/<stack>-handler` (3 days for previews, 14 days for dev; removed with the stack) |
| Table | `<stack>-items`, partition key `id` (string), on demand, removed with the stack |
| HTTP API | name `<stack>`, CORS `*`, default stage throttling: rate 10, burst 20 |
| Outputs | `ApiUrl`, `FunctionName`, `LogGroupName`, `TableName` |
| Synthesizer | `CliCredentialsStackSynthesizer` with `fileAssetsBucketName: inv-preview-assets-${AWS::AccountId}-${AWS::Region}` and `bucketPrefix: <stack>/` |
| Permissions boundary | `inv-preview-boundary` on every role (cdk.json context `@aws-cdk/core:permissionsBoundary`) |
| Deploy | from `infra/`: `npx cdk deploy --require-approval never --role-arn "$CFN_EXEC_ROLE_ARN" -c stage=<stage>` |

## 5. Workflows and artifacts

pr-triage waits for workflows by their `name:`. Every PR workflow runs on
`pull_request` with types `[opened, synchronize, reopened]`. Workflows that read the
PR description also run on `edited`.

### demo-inventory-api

| Workflow `name:` | Runs when | Artifact | Notes |
|---|---|---|---|
| `ci` | every PR, push to `dev` | none | ruff, pytest, `cdk synth` |
| `test-relevance` | PR, also `edited`; level v2 or v3 | `test-relevance` → `test-relevance.json` | fails when the verdict needs changes |
| `behavior` | PR, also `edited`; level v2 or v3 | `behavior-results` → `behavior-results.json` | runs the API with DynamoDB Local inside CI |
| `preview-deploy` | PR from the same repo; level v3 | `preview` → `preview.json` | deploys `inv-api-preview-<stage>` |
| `preview-destroy` | PR closed, same repo | none | destroys the ticket stack |
| `deploy-dev` | push to `dev` | none | deploys `inv-api-dev` |

### demo-inventory-web

| Workflow `name:` | Runs when | Artifact |
|---|---|---|
| `ci` | every PR, push to `dev` | none |
| `web-preview` | PR from the same repo; level v3 | `preview` → `preview.json` |
| `web-preview-destroy` | PR closed, same repo | none |
| `deploy-dev` | push to `dev` | none |

The level comes from the repo variable `TRIAGE_LEVEL` (`v1`, `v2`, `v3`; unset means `v3`).
Skipped jobs end as `skipped`, which pr-triage treats as "not blocking".

### `test-relevance.json`

```json
{
  "version": 1,
  "pr_type": "feature",
  "expectation": "fail_before",
  "tests": [
    {"id": "tests/test_items.py::test_item_has_stock", "before": "failed_assertion", "after": "passed"}
  ],
  "verdict": "ok",
  "message": "1 new test fails on the base code with an assertion and passes on the PR."
}
```

- `pr_type`: from the `## Type` section; when it is missing, `tests` if only `tests/` changed,
  `docs` if only Markdown changed, otherwise `feature`.
- `expectation`: `fail_before` for `feature` and `bugfix`; `pass_before` for `refactor` and `tests`.
- `before` and `after`: `passed`, `failed_assertion`, `failed_error` (import, name or attribute
  error, including collection errors) or `not_run`.
- `verdict`:
  - `ok`: for `fail_before`, at least one changed test fails on the base code with an assertion
    and passes on the PR. For `pass_before`, every changed test passes on both.
  - `undetermined`: for `fail_before`, the changed tests fail on the base code only with
    `failed_error` (for example the new function does not exist yet). The AI review then reads
    the tests.
  - `meaningless`: for `fail_before`, no changed test fails on the base code.
  - `no_tests`: `feature` or `bugfix` changed code under `src/` but no test file.
  - `unexpected_failure`: for `pass_before`, a changed test fails on the base code or on the PR.
  - `skipped`: `docs`, or nothing under `src/` or `tests/` changed.
- The job fails for `meaningless`, `no_tests` and `unexpected_failure`.

### `behavior-results.json`

```json
{
  "version": 1,
  "source": "ci-local",
  "passed": true,
  "error": null,
  "items": [
    {"id": "B1", "type": "api", "ok": true, "request": "GET /items/42", "status": 200, "request_id": "…", "detail": "status 200, body has stock"},
    {"id": "B2", "type": "log", "ok": true, "detail": "no ERROR lines for request …"},
    {"id": "B3", "type": "ui", "ok": null, "detail": "checked by a person"}
  ]
}
```

- `error` is set (and `passed` is false) when the block is missing or invalid.
- `ok` is `null` for ui items. `passed` is true when no api or log item has `ok: false`.

### `preview.json`

- api: `{"stage": "inv-123", "stack": "inv-api-preview-inv-123", "api_url": "https://…"}`
- web: `{"stage": "inv-123", "web_url": "https://<domain>/inv-123/index.html", "api_url": "https://…"}`

## 6. Repo variables

`pr-triage/scripts/configure_demo_repos.sh` sets these from the PreviewPlatform stack outputs.

| Variable | demo-inventory-api | demo-inventory-web |
|---|---|---|
| `AWS_REGION` | `ap-southeast-2` | `ap-southeast-2` |
| `PREVIEW_ROLE_ARN` | `arn:aws:iam::<acct>:role/inv-gha-preview-api` | `arn:aws:iam::<acct>:role/inv-gha-preview-web` |
| `DEV_ROLE_ARN` | `arn:aws:iam::<acct>:role/inv-gha-dev-api` | `arn:aws:iam::<acct>:role/inv-gha-dev-web` |
| `CFN_EXEC_ROLE_ARN` | `arn:aws:iam::<acct>:role/inv-preview-cfn-exec` | (not used) |
| `WEB_PREVIEW_BUCKET` | (not used) | `inv-web-preview-<acct>-<region>` |
| `WEB_PREVIEW_DOMAIN` | (not used) | the CloudFront domain, for example `d123.cloudfront.net` |
| `TRIAGE_LEVEL` | `v3` | `v3` |

GitHub OIDC trust (the `sub` claim). Newer repos put the numeric owner and repo ids in the subject,
so a renamed or re-created repo with the same name does not match. The ids are in `infra/cdk.json`
(`githubOwnerId`, `githubRepoIds`); without them the stack falls back to the name-only subject.

| Role | Trusted `sub` |
|---|---|
| `inv-gha-preview-api` | `repo:<owner>@<owner id>/demo-inventory-api@<repo id>:pull_request` |
| `inv-gha-dev-api` | `repo:<owner>@<owner id>/demo-inventory-api@<repo id>:ref:refs/heads/dev` |
| `inv-gha-preview-web` | `repo:<owner>@<owner id>/demo-inventory-web@<repo id>:pull_request` |
| `inv-gha-dev-web` | `repo:<owner>@<owner id>/demo-inventory-web@<repo id>:ref:refs/heads/dev` |

Fork PRs cannot get an OIDC token, and every deploy job also checks
`github.event.pull_request.head.repo.full_name == github.repository`.

## 7. Web preview

- One shared bucket `inv-web-preview-<acct>-<region>` behind one CloudFront distribution (OAC,
  caching disabled). PreviewPlatform in pr-triage owns both.
- A ticket preview is a prefix: `s3://<bucket>/inv-123/`. Dev is `s3://<bucket>/dev/`.
- Pages: `index.html` (item list) and `item.html?id=42` (item detail). The URL is
  `https://<domain>/inv-123/item.html?id=42`.
- `config.js` is generated at deploy time: `window.INV_CONFIG = { apiUrl: "https://…" };`.
  The API URL is the `ApiUrl` output of `inv-api-preview-<stage>` when that stack exists,
  otherwise the output of `inv-api-dev`.

Why a prefix and not a stack per ticket: a CloudFront distribution per ticket takes minutes to
create and to delete. Copying files to a prefix takes seconds and needs no CloudFront permissions
in the preview roles.

## 8. pr-triage outputs on GitHub

| Item | Value |
|---|---|
| Check run name | `pr-triage` |
| Decision labels | `triage: checking`, `triage: approvable`, `triage: changes requested`, `triage: needs human review` |
| Post-merge label | `triage: post-merge failed` |
| Label that lets a fork PR use AI once | `ai-review-ok` |
| Summary comment marker | `<!-- pr-triage:summary -->` (one comment per PR, edited in place) |
| Check conclusion | approvable → `success`, changes requested → `failure`, needs human review → `neutral` |

`neutral` passes a required check, so a "needs human review" PR still waits for a human approval
through branch protection, while "changes requested" blocks the merge until a new commit fixes it.

## 9. Branch protection on `dev` (demo repos)

- Required status checks: `ci` and `pr-triage`.
- One approving review; stale approvals are dismissed when new commits are pushed. In
  auto-approve mode pr-triage's own review (the GitHub App, with contents write) is that approval
  for the PRs it merges; every other PR still needs a person's.
- Admins may bypass (the owner is the only human in the demo).
- Squash merge only; the squash commit uses the PR title and description.
