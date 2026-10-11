import { App } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { CoreStack } from '../lib/core-stack';
import { PreviewPlatformStack } from '../lib/preview-platform-stack';

const env = { account: '111111111111', region: 'ap-southeast-2' };

function build() {
  const app = new App();
  const platform = new PreviewPlatformStack(app, 'Platform', { env, githubOwner: 'octo' });
  const core = new CoreStack(app, 'Core', {
    env,
    githubOwner: 'octo',
    managedRepos: ['demo-inventory-api', 'demo-inventory-web'],
    ticketRepo: 'demo-inventory-api',
    webPreviewDomain: platform.webPreviewDomain,
    webPreviewBucketName: platform.webPreviewBucket.bucketName,
    level: 'v3',
    mode: 'advisory',
    modelId: 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
    modelInputUsdPerMTok: 1,
    modelOutputUsdPerMTok: 5,
    allowedAuthors: ['octo'],
    dailyReviews: 10,
    monthlyAiUsd: 5,
    budgetWarnUsd: 5,
    budgetStopUsd: 8,
    code: lambda.Code.fromInline('def handler(event, context):\n    return None\n'),
  });
  return { platform: Template.fromStack(platform), core: Template.fromStack(core) };
}

const { platform, core } = build();

type Statement = { Sid?: string; Effect?: string; Action: string | string[]; Resource?: unknown; Condition?: unknown };

function policyStatements(template: Template, rolePrefix: string): Statement[] {
  const roles = template.findResources('AWS::IAM::Role');
  const roleIds = Object.keys(roles).filter((id) => id.startsWith(rolePrefix));
  const statements: Statement[] = [];
  for (const policy of Object.values(template.findResources('AWS::IAM::Policy'))) {
    const attached = (policy.Properties.Roles as { Ref: string }[]).map((r) => r.Ref);
    if (attached.some((ref) => roleIds.includes(ref))) statements.push(...policy.Properties.PolicyDocument.Statement);
  }
  for (const id of roleIds) {
    for (const inline of roles[id].Properties.Policies ?? []) statements.push(...inline.PolicyDocument.Statement);
  }
  return statements;
}

const actionsOf = (statements: Statement[]) => statements.flatMap((s) => (Array.isArray(s.Action) ? s.Action : [s.Action]));

describe('preview platform', () => {
  test('the boundary only allows the app its own table and logs', () => {
    platform.hasResourceProperties('AWS::IAM::ManagedPolicy', {
      ManagedPolicyName: 'inv-preview-boundary',
      PolicyDocument: {
        Statement: [
          Match.objectLike({ Sid: 'OwnLogs' }),
          Match.objectLike({ Sid: 'OwnTable' }),
        ],
      },
    });
  });

  test('CloudFormation may create roles only with the boundary', () => {
    const statements = policyStatements(platform, 'PreviewCfnExecRole');
    const create = statements.find((s) => s.Sid === 'CreateRolesOnlyWithTheBoundary');
    expect(create?.Condition).toEqual({
      StringEquals: { 'iam:PermissionsBoundary': { Ref: expect.stringMatching(/^PreviewBoundary/) } },
    });
    const deny = statements.find((s) => s.Sid === 'NeverDropTheBoundary');
    expect(deny?.Effect).toBe('Deny');
    expect(actionsOf(statements)).not.toContain('iam:*');
    expect(actionsOf(statements)).not.toContain('ec2:RunInstances');
  });

  test('GitHub roles trust one repo and one event type each', () => {
    const trust = (name: string, sub: string) =>
      platform.hasResourceProperties('AWS::IAM::Role', {
        RoleName: name,
        AssumeRolePolicyDocument: {
          Statement: [Match.objectLike({
            Action: 'sts:AssumeRoleWithWebIdentity',
            Condition: { StringEquals: { 'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com', 'token.actions.githubusercontent.com:sub': sub } },
          })],
        },
      });
    trust('inv-gha-preview-api', 'repo:octo/demo-inventory-api:pull_request');
    trust('inv-gha-dev-api', 'repo:octo/demo-inventory-api:ref:refs/heads/dev');
    trust('inv-gha-preview-web', 'repo:octo/demo-inventory-web:pull_request');
    trust('inv-gha-dev-web', 'repo:octo/demo-inventory-web:ref:refs/heads/dev');
  });

  test('the preview deploy role can only pass the CloudFormation role', () => {
    const statements = policyStatements(platform, 'invghapreviewapi');
    const pass = statements.find((s) => s.Sid === 'PassOnlyThePreviewRole');
    expect(pass?.Condition).toEqual({ StringEquals: { 'iam:PassedToService': 'cloudformation.amazonaws.com' } });
    expect(JSON.stringify(statements.find((s) => s.Sid === 'DeployStacks')?.Resource)).toContain('stack/inv-api-preview-inv-*/*');
  });

  test('the web preview is private behind CloudFront without caching', () => {
    platform.hasResourceProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: { BlockPublicAcls: true, BlockPublicPolicy: true, IgnorePublicAcls: true, RestrictPublicBuckets: true },
    });
    platform.resourceCountIs('AWS::CloudFront::OriginAccessControl', 1);
  });
});

describe('core', () => {
  test('the webhook receiver can only read its secret, record deliveries and queue', () => {
    const actions = actionsOf(policyStatements(core, 'IngestServiceRole'));
    expect(actions).toEqual(expect.arrayContaining(['dynamodb:PutItem', 'sqs:SendMessage']));
    expect(actions.some((a) => a.startsWith('bedrock:') || a.startsWith('states:'))).toBe(false);
    const delivery = policyStatements(core, 'IngestServiceRole').find((s) => s.Sid === 'OnlyDeliveryRecords');
    expect(delivery?.Condition).toEqual({ 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['DELIVERY#*'] } });
  });

  test('only the review step may call the model, and only the configured one', () => {
    const roles = Object.keys(core.findResources('AWS::IAM::Role'));
    for (const role of roles) {
      const bedrock = actionsOf(policyStatements(core, role)).filter((a) => a.startsWith('bedrock:'));
      if (role.startsWith('ReviewServiceRole')) {
        expect(bedrock).toEqual(['bedrock:InvokeModel']);
      } else {
        expect(bedrock).toEqual([]);
      }
    }
    const statement = policyStatements(core, 'ReviewServiceRole').find((s) => s.Sid === 'OnlyTheReviewModel');
    expect(JSON.stringify(statement?.Resource)).toContain('foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0');
    expect(JSON.stringify(statement?.Resource)).toContain('inference-profile/global.anthropic.claude-haiku-4-5-20251001-v1:0');
  });

  test('the workflow waits for CI with a task token and a time limit', () => {
    const [machine] = Object.values(core.findResources('AWS::StepFunctions::StateMachine'));
    const definition = JSON.stringify(machine.Properties.DefinitionString);
    expect(definition).toContain('waitForTaskToken');
    expect(definition).toContain('\\"TimeoutSeconds\\":3600');
    expect(definition).toContain('States.Timeout');
    expect(definition).toContain('PublishFailure');
  });

  test('money: a budget with a warning and a stop, and limits in SSM', () => {
    core.hasResourceProperties('AWS::Budgets::Budget', {
      Budget: { BudgetLimit: { Amount: 8, Unit: 'USD' }, TimeUnit: 'MONTHLY', BudgetType: 'COST' },
      NotificationsWithSubscribers: [
        Match.objectLike({ Notification: Match.objectLike({ Threshold: 5 }) }),
        Match.objectLike({ Notification: Match.objectLike({ Threshold: 8 }) }),
      ],
    });
    core.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/pr-triage/config/limits',
      Value: JSON.stringify({ daily_reviews: 10, monthly_usd: 5, max_input_tokens: 30000, max_output_tokens: 3000 }),
    });
  });

  test('evidence expires after 90 days and the table expires waits', () => {
    core.hasResourceProperties('AWS::S3::Bucket', {
      LifecycleConfiguration: { Rules: [Match.objectLike({ ExpirationInDays: 90 })] },
    });
    core.hasResourceProperties('AWS::DynamoDB::Table', {
      TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
    });
  });
});
