/** Names shared with the demo repos. See docs/contracts.md before changing any of them. */
export const NAMES = {
  boundaryPolicy: 'inv-preview-boundary',
  cfnExecRole: 'inv-preview-cfn-exec',
  ghaPreviewApi: 'inv-gha-preview-api',
  ghaDevApi: 'inv-gha-dev-api',
  ghaPreviewWeb: 'inv-gha-preview-web',
  ghaDevWeb: 'inv-gha-dev-web',
  previewStacks: 'inv-api-preview-inv-*',
  devStack: 'inv-api-dev',
  /** Every resource of the demo API stacks starts with this. */
  apiPrefix: 'inv-api-',
  paramPrefix: '/pr-triage',
  table: 'pr-triage-state',
  stateMachine: 'pr-triage-review',
} as const;

export const GITHUB_OIDC_URL = 'token.actions.githubusercontent.com';
