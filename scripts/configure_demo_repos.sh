#!/usr/bin/env bash
# Configure the two demo repos from the deployed stacks: repo variables, labels, merge settings,
# branch protection on dev, and approval for Actions runs from outside contributors.
#
#   scripts/configure_demo_repos.sh [--profile jae-dev]
#
# Needs: gh (logged in with repo scope), aws, the pr-triage stacks deployed, the GitHub App created.
set -euo pipefail

OWNER="${OWNER:-jaeneungsim}"
REGION="${REGION:-ap-southeast-2}"
AWS_ARGS=(--region "$REGION")
if [[ "${1:-}" == "--profile" ]]; then AWS_ARGS+=(--profile "$2"); fi

output() {
  aws cloudformation describe-stacks --stack-name pr-triage-preview-platform "${AWS_ARGS[@]}" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
APP_ID="$(aws ssm get-parameter --name /pr-triage/github/app-id "${AWS_ARGS[@]}" --query Parameter.Value --output text)"
API="$OWNER/demo-inventory-api"
WEB="$OWNER/demo-inventory-web"

echo "Repo variables"
gh variable set AWS_REGION -R "$API" -b "$REGION"
gh variable set PREVIEW_ROLE_ARN -R "$API" -b "$(output PreviewApiRoleArn)"
gh variable set DEV_ROLE_ARN -R "$API" -b "$(output DevApiRoleArn)"
gh variable set CFN_EXEC_ROLE_ARN -R "$API" -b "$(output CfnExecRoleArn)"
gh variable set AWS_REGION -R "$WEB" -b "$REGION"
gh variable set PREVIEW_ROLE_ARN -R "$WEB" -b "$(output PreviewWebRoleArn)"
gh variable set DEV_ROLE_ARN -R "$WEB" -b "$(output DevWebRoleArn)"
gh variable set WEB_PREVIEW_BUCKET -R "$WEB" -b "$(output WebPreviewBucketName)"
gh variable set WEB_PREVIEW_DOMAIN -R "$WEB" -b "$(output WebPreviewDomain)"
LEVEL="$(aws ssm get-parameter --name /pr-triage/config/level "${AWS_ARGS[@]}" --query Parameter.Value --output text)"
for repo in "$API" "$WEB"; do gh variable set TRIAGE_LEVEL -R "$repo" -b "$LEVEL"; done

for repo in "$API" "$WEB"; do
  echo "Labels and settings for $repo"
  gh label create "triage: checking" -R "$repo" --color c5def5 --description "pr-triage is reviewing this commit" --force
  gh label create "triage: approvable" -R "$repo" --color 0e8a16 --description "All checks passed; a person approves" --force
  gh label create "triage: changes requested" -R "$repo" --color d93f0b --description "The author has something to fix" --force
  gh label create "triage: needs human review" -R "$repo" --color fbca04 --description "A person has to look" --force
  gh label create "triage: post-merge failed" -R "$repo" --color b60205 --description "The merged change misbehaves on dev" --force
  gh label create "ai-review-ok" -R "$repo" --color 5319e7 --description "Maintainer allows one AI review of a fork PR commit" --force

  gh api -X PATCH "repos/$repo" --silent \
    -F allow_squash_merge=true -F allow_merge_commit=false -F allow_rebase_merge=false \
    -f squash_merge_commit_title=PR_TITLE -f squash_merge_commit_message=PR_BODY -F delete_branch_on_merge=true

  # Only this GitHub App can satisfy the pr-triage check; a workflow cannot fake it.
  gh api -X PUT "repos/$repo/branches/dev/protection" --silent --input - <<JSON
{
  "required_status_checks": {"strict": false, "checks": [{"context": "ci"}, {"context": "pr-triage", "app_id": $APP_ID}]},
  "enforce_admins": false,
  "required_pull_request_reviews": {"dismiss_stale_reviews": true, "required_approving_review_count": 1},
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON

  gh api -X PUT "repos/$repo/actions/permissions/fork-pr-contributor-approval" --silent \
    -f approval_policy=all_external_contributors \
    || echo "  Set 'Require approval for all external contributors' in Settings > Actions by hand."
done
echo "Done."
