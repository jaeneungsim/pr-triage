#!/usr/bin/env bash
# Switch the review level for pr-triage and both demo repos: v1 (read code), v2 (+ run in CI),
# v3 (+ run on an AWS preview per ticket).
#
#   scripts/set_level.sh v2 [--profile jae-dev]
set -euo pipefail

LEVEL="${1:?usage: set_level.sh v1|v2|v3 [--profile name]}"
[[ "$LEVEL" =~ ^v[123]$ ]] || { echo "level must be v1, v2 or v3" >&2; exit 1; }
OWNER="${OWNER:-jaeneungsim}"
AWS_ARGS=(--region "${REGION:-ap-southeast-2}")
if [[ "${2:-}" == "--profile" ]]; then AWS_ARGS+=(--profile "$3"); fi

aws ssm put-parameter --name /pr-triage/config/level --value "$LEVEL" --type String --overwrite "${AWS_ARGS[@]}" >/dev/null
for repo in demo-inventory-api demo-inventory-web; do
  gh variable set TRIAGE_LEVEL -R "$OWNER/$repo" -b "$LEVEL"
done
echo "Level is now $LEVEL. Reviews that already started keep the level they started with."
