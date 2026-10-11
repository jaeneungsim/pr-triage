import { CfnOutput, Duration, RemovalPolicy, Stack, StackProps } from 'aws-cdk-lib';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';
import { GITHUB_OIDC_URL, NAMES } from './names';

export interface PreviewPlatformProps extends StackProps {
  readonly githubOwner: string;
  /**
   * GitHub's numeric owner and repo ids. Newer repos get OIDC subjects such as
   * `repo:owner@123/name@456:ref:refs/heads/dev`; with the ids, a renamed or re-created repo
   * of the same name cannot assume the roles. Without them the old name-only subject is used.
   */
  readonly githubOwnerId?: string;
  readonly githubRepoIds?: Record<string, string>;
  /** Reuse the account's GitHub OIDC provider when it already exists (one per account). */
  readonly githubOidcProviderArn?: string;
}

export function githubSubject(owner: string, repo: string, subject: string, ownerId?: string, repoId?: string): string {
  return ownerId && repoId ? `repo:${owner}@${ownerId}/${repo}@${repoId}:${subject}` : `repo:${owner}/${repo}:${subject}`;
}

const CFN_DEPLOY_ACTIONS = [
  'cloudformation:CreateChangeSet',
  'cloudformation:DescribeChangeSet',
  'cloudformation:ExecuteChangeSet',
  'cloudformation:DeleteChangeSet',
  'cloudformation:ListChangeSets',
  'cloudformation:DescribeStacks',
  'cloudformation:DescribeStackEvents',
  'cloudformation:DescribeStackResources',
  'cloudformation:ListStackResources',
  'cloudformation:GetTemplate',
  'cloudformation:GetTemplateSummary',
  'cloudformation:DeleteStack',
  'cloudformation:ContinueUpdateRollback',
  'cloudformation:RollbackStack',
];

/**
 * Same-account isolation for ticket previews and dev of the demo app.
 *
 * GitHub Actions role -> may only touch `inv-api-*` stacks and pass one CloudFormation role.
 * CloudFormation role -> may only create the few services the demo app uses, under `inv-api-*`
 *                        names, and every IAM role it creates must carry the boundary.
 * Boundary            -> caps what the app's own roles can ever do: its table and its logs.
 */
export class PreviewPlatformStack extends Stack {
  readonly webPreviewBucket: s3.Bucket;
  readonly webPreviewDomain: string;

  constructor(scope: Construct, id: string, props: PreviewPlatformProps) {
    super(scope, id, props);
    const { account, region, partition } = this;
    const arn = (service: string, resource: string) => `arn:${partition}:${service}:${region}:${account}:${resource}`;
    const stack = (name: string) => arn('cloudformation', `stack/${name}/*`);
    const apiFunctions = arn('lambda', `function:${NAMES.apiPrefix}*`);
    const apiRoles = `arn:${partition}:iam::${account}:role/${NAMES.apiPrefix}*`;
    const apiTables = arn('dynamodb', `table/${NAMES.apiPrefix}*`);
    const apiLogs = arn('logs', `log-group:/aws/lambda/${NAMES.apiPrefix}*`);

    const boundary = new iam.ManagedPolicy(this, 'PreviewBoundary', {
      managedPolicyName: NAMES.boundaryPolicy,
      description: 'Upper limit for every role that a demo API preview or dev stack creates.',
      statements: [
        new iam.PolicyStatement({
          sid: 'OwnLogs',
          actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
          resources: [apiLogs, `${apiLogs}:*`],
        }),
        new iam.PolicyStatement({
          sid: 'OwnTable',
          actions: [
            'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem',
            'dynamodb:Query', 'dynamodb:Scan', 'dynamodb:BatchGetItem', 'dynamodb:BatchWriteItem',
            'dynamodb:ConditionCheckItem', 'dynamodb:DescribeTable',
          ],
          resources: [apiTables, `${apiTables}/index/*`],
        }),
      ],
    });

    const assets = new s3.Bucket(this, 'PreviewAssets', {
      bucketName: `inv-preview-assets-${account}-${region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      lifecycleRules: [{ expiration: Duration.days(14) }],
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const execRole = new iam.Role(this, 'PreviewCfnExecRole', {
      roleName: NAMES.cfnExecRole,
      description: 'CloudFormation deploys demo API previews and dev with this role only.',
      assumedBy: new iam.ServicePrincipal('cloudformation.amazonaws.com'),
      inlinePolicies: {
        deploy: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              sid: 'Functions',
              actions: [
                'lambda:CreateFunction', 'lambda:DeleteFunction', 'lambda:UpdateFunctionCode',
                'lambda:UpdateFunctionConfiguration', 'lambda:Get*', 'lambda:List*', 'lambda:AddPermission',
                'lambda:RemovePermission', 'lambda:TagResource', 'lambda:UntagResource', 'lambda:InvokeFunction',
                'lambda:PutFunctionConcurrency', 'lambda:DeleteFunctionConcurrency', 'lambda:PublishVersion',
                'lambda:PutRuntimeManagementConfig', 'lambda:PutFunctionRecursionConfig',
              ],
              resources: [apiFunctions],
            }),
            new iam.PolicyStatement({
              sid: 'CreateRolesOnlyWithTheBoundary',
              actions: ['iam:CreateRole', 'iam:PutRolePermissionsBoundary'],
              resources: [apiRoles],
              conditions: { StringEquals: { 'iam:PermissionsBoundary': boundary.managedPolicyArn } },
            }),
            new iam.PolicyStatement({
              sid: 'ManageOwnRoles',
              actions: [
                'iam:DeleteRole', 'iam:GetRole', 'iam:TagRole', 'iam:UntagRole', 'iam:UpdateRole',
                'iam:UpdateRoleDescription', 'iam:UpdateAssumeRolePolicy', 'iam:PutRolePolicy',
                'iam:DeleteRolePolicy', 'iam:GetRolePolicy', 'iam:AttachRolePolicy', 'iam:DetachRolePolicy',
                'iam:ListRolePolicies', 'iam:ListAttachedRolePolicies',
              ],
              resources: [apiRoles],
            }),
            new iam.PolicyStatement({
              sid: 'PassOwnRolesToLambda',
              actions: ['iam:PassRole'],
              resources: [apiRoles],
              conditions: { StringEquals: { 'iam:PassedToService': 'lambda.amazonaws.com' } },
            }),
            new iam.PolicyStatement({
              sid: 'NeverDropTheBoundary',
              effect: iam.Effect.DENY,
              actions: ['iam:DeleteRolePermissionsBoundary', 'iam:CreateUser', 'iam:CreateAccessKey'],
              resources: ['*'],
            }),
            new iam.PolicyStatement({
              sid: 'Tables',
              actions: [
                'dynamodb:CreateTable', 'dynamodb:DeleteTable', 'dynamodb:UpdateTable', 'dynamodb:Describe*',
                'dynamodb:ListTagsOfResource', 'dynamodb:TagResource', 'dynamodb:UntagResource',
                'dynamodb:UpdateContinuousBackups', 'dynamodb:UpdateTimeToLive', 'dynamodb:GetResourcePolicy',
                'dynamodb:UpdateContributorInsights',
              ],
              resources: [apiTables],
            }),
            new iam.PolicyStatement({
              sid: 'HttpApis',
              actions: ['apigateway:GET', 'apigateway:POST', 'apigateway:PUT', 'apigateway:PATCH', 'apigateway:DELETE'],
              resources: [
                `arn:${partition}:apigateway:${region}::/apis`,
                `arn:${partition}:apigateway:${region}::/apis/*`,
                `arn:${partition}:apigateway:${region}::/tags/*`,
              ],
            }),
            new iam.PolicyStatement({
              sid: 'LogGroups',
              actions: [
                'logs:CreateLogGroup', 'logs:DeleteLogGroup', 'logs:PutRetentionPolicy', 'logs:DeleteRetentionPolicy',
                'logs:TagResource', 'logs:UntagResource', 'logs:TagLogGroup', 'logs:UntagLogGroup',
                'logs:ListTagsForResource', 'logs:ListTagsLogGroup',
              ],
              resources: [apiLogs, `${apiLogs}:*`],
            }),
            new iam.PolicyStatement({ sid: 'DescribeLogGroups', actions: ['logs:DescribeLogGroups'], resources: ['*'] }),
            new iam.PolicyStatement({
              sid: 'ReadAssets',
              actions: ['s3:GetObject'],
              resources: [assets.arnForObjects(`${NAMES.apiPrefix}*`)],
            }),
          ],
        }),
      },
    });

    const web = new s3.Bucket(this, 'WebPreview', {
      bucketName: `inv-web-preview-${account}-${region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });
    const distribution = new cloudfront.Distribution(this, 'WebPreviewDistribution', {
      comment: 'Ticket previews and dev of demo-inventory-web',
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(web),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
      },
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
    });
    this.webPreviewBucket = web;
    this.webPreviewDomain = distribution.distributionDomainName;

    const providerArn =
      props.githubOidcProviderArn ||
      new iam.CfnOIDCProvider(this, 'GitHubOidc', {
        url: `https://${GITHUB_OIDC_URL}`,
        clientIdList: ['sts.amazonaws.com'],
        // IAM trusts GitHub's certificate authority itself; the thumbprints are kept for older checks.
        thumbprintList: ['6938fd4d98bab03faadb97b34396831e3780aea1', '1c58a3a8518e8759bf075b76b750d4f2df264fcd'],
      }).attrArn;
    const githubRole = (name: string, repo: string, subject: string, description: string) =>
      new iam.Role(this, name, {
        roleName: name,
        description,
        maxSessionDuration: Duration.hours(1),
        assumedBy: new iam.WebIdentityPrincipal(providerArn, {
          StringEquals: {
            [`${GITHUB_OIDC_URL}:aud`]: 'sts.amazonaws.com',
            [`${GITHUB_OIDC_URL}:sub`]: githubSubject(
              props.githubOwner, repo, subject, props.githubOwnerId, props.githubRepoIds?.[repo],
            ),
          },
        }),
      });

    const apiDeployer = (name: string, subject: string, stackName: string, description: string) => {
      const role = githubRole(name, 'demo-inventory-api', subject, description);
      role.addToPolicy(new iam.PolicyStatement({ sid: 'DeployStacks', actions: CFN_DEPLOY_ACTIONS, resources: [stack(stackName)] }));
      role.addToPolicy(new iam.PolicyStatement({
        sid: 'ReadToolkit',
        actions: ['cloudformation:DescribeStacks'],
        resources: [stack('CDKToolkit')],
      }));
      role.addToPolicy(new iam.PolicyStatement({
        sid: 'PassOnlyThePreviewRole',
        actions: ['iam:PassRole'],
        resources: [execRole.roleArn],
        conditions: { StringEquals: { 'iam:PassedToService': 'cloudformation.amazonaws.com' } },
      }));
      role.addToPolicy(new iam.PolicyStatement({
        sid: 'UploadAssets',
        actions: ['s3:PutObject', 's3:GetObject'],
        resources: [assets.arnForObjects(`${stackName}/*`)],
      }));
      role.addToPolicy(new iam.PolicyStatement({
        sid: 'FindAssetBucket',
        actions: ['s3:ListBucket', 's3:GetBucketLocation', 's3:GetEncryptionConfiguration'],
        resources: [assets.bucketArn],
      }));
      return role;
    };
    const previewApi = apiDeployer(NAMES.ghaPreviewApi, 'pull_request', NAMES.previewStacks,
      'GitHub Actions in demo-inventory-api PRs: deploy and destroy ticket previews.');
    const devApi = apiDeployer(NAMES.ghaDevApi, 'ref:refs/heads/dev', NAMES.devStack,
      'GitHub Actions on pushes to dev of demo-inventory-api: deploy dev.');

    const webDeployer = (name: string, subject: string, prefix: string, stacks: string[], description: string) => {
      const role = githubRole(name, 'demo-inventory-web', subject, description);
      role.addToPolicy(new iam.PolicyStatement({
        sid: 'ListOwnPrefix',
        actions: ['s3:ListBucket'],
        resources: [web.bucketArn],
        conditions: { StringLike: { 's3:prefix': [`${prefix}*`] } },
      }));
      role.addToPolicy(new iam.PolicyStatement({
        sid: 'WriteOwnPrefix',
        actions: ['s3:PutObject', 's3:GetObject', 's3:DeleteObject'],
        resources: [web.arnForObjects(`${prefix}*`)],
      }));
      role.addToPolicy(new iam.PolicyStatement({
        sid: 'ReadApiUrl',
        actions: ['cloudformation:DescribeStacks'],
        resources: stacks.map(stack),
      }));
      return role;
    };
    const previewWeb = webDeployer(NAMES.ghaPreviewWeb, 'pull_request', 'inv-',
      [NAMES.previewStacks, NAMES.devStack], 'GitHub Actions in demo-inventory-web PRs: publish ticket previews.');
    const devWeb = webDeployer(NAMES.ghaDevWeb, 'ref:refs/heads/dev', 'dev/', [NAMES.devStack],
      'GitHub Actions on pushes to dev of demo-inventory-web: publish dev.');

    const out = (id: string, value: string, description: string) => new CfnOutput(this, id, { value, description });
    out('PreviewApiRoleArn', previewApi.roleArn, 'demo-inventory-api variable PREVIEW_ROLE_ARN');
    out('DevApiRoleArn', devApi.roleArn, 'demo-inventory-api variable DEV_ROLE_ARN');
    out('CfnExecRoleArn', execRole.roleArn, 'demo-inventory-api variable CFN_EXEC_ROLE_ARN');
    out('PreviewWebRoleArn', previewWeb.roleArn, 'demo-inventory-web variable PREVIEW_ROLE_ARN');
    out('DevWebRoleArn', devWeb.roleArn, 'demo-inventory-web variable DEV_ROLE_ARN');
    out('WebPreviewBucketName', web.bucketName, 'demo-inventory-web variable WEB_PREVIEW_BUCKET');
    out('WebPreviewDomain', distribution.distributionDomainName, 'demo-inventory-web variable WEB_PREVIEW_DOMAIN');
    out('AssetsBucketName', assets.bucketName, 'Where preview and dev deploys upload their assets');
    out('BoundaryPolicyArn', boundary.managedPolicyArn, 'Permissions boundary of every demo API role');
  }
}
