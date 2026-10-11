import { CfnOutput, Duration, RemovalPolicy, Stack, StackProps } from 'aws-cdk-lib';
import * as apigw from 'aws-cdk-lib/aws-apigatewayv2';
import { HttpLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as budgets from 'aws-cdk-lib/aws-budgets';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { SqsEventSource } from 'aws-cdk-lib/aws-lambda-event-sources';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as subscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { NAMES } from './names';
import { pythonCode } from './python-code';
import { ReviewStateMachine } from './review-state-machine';

export interface CoreProps extends StackProps {
  readonly githubOwner: string;
  readonly managedRepos: string[];
  readonly ticketRepo: string;
  readonly webPreviewDomain: string;
  readonly webPreviewBucketName: string;
  readonly level: string;
  readonly mode: string;
  readonly modelId: string;
  readonly modelInputUsdPerMTok: number;
  readonly modelOutputUsdPerMTok: number;
  readonly allowedAuthors: string[];
  readonly dailyReviews: number;
  readonly monthlyAiUsd: number;
  readonly budgetWarnUsd: number;
  readonly budgetStopUsd: number;
  readonly alertEmail?: string;
  /** Tests skip bundling; deploys bundle the Python code with its dependencies. */
  readonly code?: lambda.Code;
}

const PROFILE_PREFIX = /^(global|us|eu|apac|au|jp|ca|us-gov)\.(.+)$/;

/** The review service: webhook in, one Step Functions execution per commit, results out. */
export class CoreStack extends Stack {
  constructor(scope: Construct, id: string, props: CoreProps) {
    super(scope, id, props);
    const { account, region, partition } = this;
    const param = (path: string) => `arn:${partition}:ssm:${region}:${account}:parameter${NAMES.paramPrefix}/${path}`;
    const stackArn = (name: string) => `arn:${partition}:cloudformation:${region}:${account}:stack/${name}/*`;
    const apiLogGroups = (stage: string) => `arn:${partition}:logs:${region}:${account}:log-group:/aws/lambda/${stage}*`;

    const table = new dynamodb.Table(this, 'State', {
      tableName: NAMES.table,
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: RemovalPolicy.DESTROY,
    });
    table.addGlobalSecondaryIndex({
      indexName: 'gsi1',
      partitionKey: { name: 'gsi1pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'gsi1sk', type: dynamodb.AttributeType.STRING },
    });

    const evidence = new s3.Bucket(this, 'Evidence', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      lifecycleRules: [{ expiration: Duration.days(90) }],
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const deadLetters = new sqs.Queue(this, 'EventsDLQ', {
      retentionPeriod: Duration.days(14),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
    });
    const queue = new sqs.Queue(this, 'Events', {
      visibilityTimeout: Duration.minutes(6),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      deadLetterQueue: { queue: deadLetters, maxReceiveCount: 3 },
    });

    const config = (name: string, value: string, description: string) =>
      new ssm.StringParameter(this, `Config-${name}`, {
        parameterName: `${NAMES.paramPrefix}/config/${name}`,
        stringValue: value,
        description,
      });
    config('enabled', 'on', 'on or off. The budget alarm sets it to off; a person sets it back.');
    config('level', props.level, 'v1, v2 or v3');
    config('mode', props.mode, 'advisory or auto-approve');
    config('limits', JSON.stringify({
      daily_reviews: props.dailyReviews,
      monthly_usd: props.monthlyAiUsd,
      max_input_tokens: 30000,
      max_output_tokens: 3000,
    }), 'AI usage limits, counted by the app');
    config('model', JSON.stringify({
      id: props.modelId,
      input_usd_per_mtok: props.modelInputUsdPerMTok,
      output_usd_per_mtok: props.modelOutputUsdPerMTok,
    }), 'Bedrock model or inference profile, and its price per million tokens');
    config('allowed-authors', JSON.stringify(props.allowedAuthors), 'GitHub logins whose PRs may use the AI review');

    const code = props.code ?? pythonCode();
    const commonEnv = {
      PARAM_PREFIX: NAMES.paramPrefix,
      TABLE_NAME: table.tableName,
      EVIDENCE_BUCKET: evidence.bucketName,
      GITHUB_OWNER: props.githubOwner,
      MANAGED_REPOS: props.managedRepos.join(','),
      WEB_PREVIEW_DOMAIN: props.webPreviewDomain,
    };
    const fn = (id: string, handler: string, timeout: number, environment: Record<string, string> = {}, memorySize = 256) =>
      new lambda.Function(this, id, {
        code,
        handler: `pr_triage.handlers.${handler}.handler`,
        runtime: lambda.Runtime.PYTHON_3_13,
        architecture: lambda.Architecture.ARM_64,
        memorySize,
        timeout: Duration.seconds(timeout),
        environment: { ...commonEnv, ...environment },
        loggingFormat: lambda.LoggingFormat.JSON,
        logGroup: new logs.LogGroup(this, `${id}Logs`, {
          retention: logs.RetentionDays.TWO_WEEKS,
          removalPolicy: RemovalPolicy.DESTROY,
        }),
      });

    const githubSecrets = new iam.PolicyStatement({
      actions: ['ssm:GetParameter', 'ssm:GetParameters'],
      resources: [param('github/app-id'), param('github/private-key')],
    });
    const readConfig = new iam.PolicyStatement({
      actions: ['ssm:GetParameter', 'ssm:GetParameters'],
      resources: [param('config/*')],
    });

    // --- the review steps ---------------------------------------------------------------------
    const context = fn('Context', 'context', 60, {}, 512);
    const policy = fn('Policy', 'policy_step', 15);
    const waitCi = fn('WaitCi', 'wait_ci', 30);
    const verify = fn('Verify', 'verify', 120);
    const review = fn('Review', 'review', 120);
    const validate = fn('Validate', 'validate_step', 30);
    const decide = fn('Decide', 'decide_step', 10);
    const publish = fn('Publish', 'publish', 60);
    const publishFailure = fn('PublishFailure', 'publish_failure', 30);

    for (const f of [context, waitCi, verify, publish, publishFailure]) f.addToRolePolicy(githubSecrets);
    for (const f of [context, policy, review, publish]) f.addToRolePolicy(readConfig);
    for (const f of [context, waitCi, review, publish, publishFailure]) table.grantReadWriteData(f);
    evidence.grantPut(context);
    for (const f of [policy, verify, review, validate, publish]) evidence.grantReadWrite(f);

    const previewStacks = [stackArn(NAMES.previewStacks), stackArn(NAMES.devStack)];
    verify.addToRolePolicy(new iam.PolicyStatement({ actions: ['cloudformation:DescribeStacks'], resources: previewStacks }));
    verify.addToRolePolicy(new iam.PolicyStatement({
      actions: ['logs:FilterLogEvents'],
      resources: [apiLogGroups(NAMES.apiPrefix), `${apiLogGroups(NAMES.apiPrefix)}:*`],
    }));

    const profile = PROFILE_PREFIX.exec(props.modelId);
    const baseModel = profile ? profile[2] : props.modelId;
    review.addToRolePolicy(new iam.PolicyStatement({
      sid: 'OnlyTheReviewModel',
      actions: ['bedrock:InvokeModel'],
      resources: [
        `arn:${partition}:bedrock:*::foundation-model/${baseModel}`,
        ...(profile ? [`arn:${partition}:bedrock:${region}:${account}:inference-profile/${props.modelId}`] : []),
      ],
    }));

    const workflow = new ReviewStateMachine(this, 'ReviewWorkflow', {
      steps: { context, policy, waitCi, verify, review, validate, decide, publish, publishFailure },
      ciWait: Duration.hours(1),
    });
    const stateMachine = workflow.stateMachine;
    // The workflow invokes WaitCi, so WaitCi's policy names the workflow by its fixed ARN
    // instead of a reference; a reference would make the two depend on each other.
    waitCi.addToRolePolicy(new iam.PolicyStatement({
      actions: ['states:SendTaskSuccess', 'states:SendTaskFailure', 'states:SendTaskHeartbeat'],
      resources: [`arn:${partition}:states:${region}:${account}:stateMachine:${NAMES.stateMachine}`],
    }));

    // --- webhook in -----------------------------------------------------------------------------
    const ingest = fn('Ingest', 'ingest', 10, { QUEUE_URL: queue.queueUrl });
    ingest.addToRolePolicy(new iam.PolicyStatement({
      actions: ['ssm:GetParameter', 'ssm:GetParameters'],
      resources: [param('github/webhook-secret')],
    }));
    ingest.addToRolePolicy(new iam.PolicyStatement({
      sid: 'OnlyDeliveryRecords',
      actions: ['dynamodb:PutItem', 'dynamodb:DeleteItem'],
      resources: [table.tableArn],
      conditions: { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['DELIVERY#*'] } },
    }));
    queue.grantSendMessages(ingest);

    const api = new apigw.HttpApi(this, 'WebhookApi', {
      apiName: 'pr-triage-webhook',
      createDefaultStage: false,
    });
    api.addRoutes({
      path: '/webhook',
      methods: [apigw.HttpMethod.POST],
      integration: new HttpLambdaIntegration('Ingest', ingest),
    });
    new apigw.HttpStage(this, 'WebhookStage', {
      httpApi: api,
      stageName: '$default',
      autoDeploy: true,
      throttle: { rateLimit: 5, burstLimit: 10 },
    });

    const postmerge = fn('PostMerge', 'postmerge', 90);
    postmerge.addToRolePolicy(githubSecrets);
    postmerge.addToRolePolicy(new iam.PolicyStatement({
      actions: ['cloudformation:DescribeStacks'],
      resources: [stackArn(NAMES.devStack)],
    }));
    postmerge.addToRolePolicy(new iam.PolicyStatement({
      actions: ['logs:FilterLogEvents'],
      resources: [apiLogGroups(NAMES.devStack), `${apiLogGroups(NAMES.devStack)}:*`],
    }));

    const dispatcher = fn('Dispatcher', 'dispatcher', 60, {
      STATE_MACHINE_ARN: stateMachine.stateMachineArn,
      QUEUE_URL: queue.queueUrl,
      POSTMERGE_FUNCTION: postmerge.functionName,
    });
    dispatcher.addEventSource(new SqsEventSource(queue, { batchSize: 5, reportBatchItemFailures: true, maxConcurrency: 2 }));
    dispatcher.addToRolePolicy(githubSecrets);
    table.grantReadWriteData(dispatcher);
    queue.grantSendMessages(dispatcher);
    stateMachine.grantStartExecution(dispatcher);
    stateMachine.grantTaskResponse(dispatcher);
    stateMachine.grantRead(dispatcher);
    dispatcher.addToRolePolicy(new iam.PolicyStatement({
      actions: ['states:StopExecution'],
      resources: [`arn:${partition}:states:${region}:${account}:execution:${NAMES.stateMachine}:*`],
    }));
    postmerge.grantInvoke(dispatcher);

    // --- scheduled jobs -------------------------------------------------------------------------
    const digest = fn('Digest', 'digest', 60);
    digest.addToRolePolicy(githubSecrets);
    table.grantReadWriteData(digest);

    const cleanup = fn('Cleanup', 'cleanup', 120, { WEB_PREVIEW_BUCKET: props.webPreviewBucketName });
    table.grantReadData(cleanup);
    cleanup.addToRolePolicy(new iam.PolicyStatement({ actions: ['cloudformation:ListStacks'], resources: ['*'] }));
    cleanup.addToRolePolicy(new iam.PolicyStatement({
      actions: ['cloudformation:DeleteStack', 'cloudformation:DescribeStacks'],
      resources: [stackArn(NAMES.previewStacks)],
    }));
    const webBucketArn = `arn:${partition}:s3:::${props.webPreviewBucketName}`;
    cleanup.addToRolePolicy(new iam.PolicyStatement({ actions: ['s3:ListBucket'], resources: [webBucketArn] }));
    cleanup.addToRolePolicy(new iam.PolicyStatement({ actions: ['s3:DeleteObject'], resources: [`${webBucketArn}/inv-*`] }));

    const schedulerRole = new iam.Role(this, 'SchedulerRole', {
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com', {
        conditions: { StringEquals: { 'aws:SourceAccount': account } },
      }),
    });
    digest.grantInvoke(schedulerRole);
    cleanup.grantInvoke(schedulerRole);
    new scheduler.CfnSchedule(this, 'MorningDigest', {
      description: 'Summary of the PRs reviewed overnight',
      scheduleExpression: 'cron(0 8 ? * MON-FRI *)',
      scheduleExpressionTimezone: 'Australia/Sydney',
      flexibleTimeWindow: { mode: 'OFF' },
      target: { arn: digest.functionArn, roleArn: schedulerRole.roleArn },
    });
    new scheduler.CfnSchedule(this, 'PreviewCleanup', {
      description: 'Delete ticket previews older than a day or without an open PR',
      scheduleExpression: 'rate(1 hour)',
      flexibleTimeWindow: { mode: 'OFF' },
      target: { arn: cleanup.functionArn, roleArn: schedulerRole.roleArn },
    });

    // --- money and alarms -----------------------------------------------------------------------
    const alerts = new sns.Topic(this, 'Alerts', { displayName: 'pr-triage alerts' });
    const stop = new sns.Topic(this, 'BudgetStop', { displayName: 'pr-triage budget stop' });
    for (const topic of [alerts, stop]) {
      topic.addToResourcePolicy(new iam.PolicyStatement({
        principals: [new iam.ServicePrincipal('budgets.amazonaws.com')],
        actions: ['sns:Publish'],
        resources: [topic.topicArn],
        conditions: { StringEquals: { 'aws:SourceAccount': account } },
      }));
    }
    if (props.alertEmail) alerts.addSubscription(new subscriptions.EmailSubscription(props.alertEmail));

    const killswitch = fn('Killswitch', 'killswitch', 10);
    killswitch.addToRolePolicy(new iam.PolicyStatement({ actions: ['ssm:PutParameter'], resources: [param('config/enabled')] }));
    stop.addSubscription(new subscriptions.LambdaSubscription(killswitch));

    const email = props.alertEmail ? [{ subscriptionType: 'EMAIL', address: props.alertEmail }] : [];
    new budgets.CfnBudget(this, 'AccountBudget', {
      budget: {
        budgetName: 'pr-triage-account-monthly',
        budgetType: 'COST',
        timeUnit: 'MONTHLY',
        budgetLimit: { amount: props.budgetStopUsd, unit: 'USD' },
      },
      notificationsWithSubscribers: [
        {
          notification: { notificationType: 'ACTUAL', comparisonOperator: 'GREATER_THAN', threshold: props.budgetWarnUsd, thresholdType: 'ABSOLUTE_VALUE' },
          subscribers: [{ subscriptionType: 'SNS', address: alerts.topicArn }, ...email],
        },
        {
          notification: { notificationType: 'ACTUAL', comparisonOperator: 'GREATER_THAN', threshold: props.budgetStopUsd, thresholdType: 'ABSOLUTE_VALUE' },
          subscribers: [{ subscriptionType: 'SNS', address: stop.topicArn }, ...email],
        },
      ],
    });

    const alarm = (id: string, metric: cloudwatch.IMetric, description: string) =>
      new cloudwatch.Alarm(this, id, {
        metric,
        threshold: 1,
        evaluationPeriods: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        alarmDescription: description,
      }).addAlarmAction(new actions.SnsAction(alerts));
    alarm('DeadLetters', deadLetters.metricApproximateNumberOfMessagesVisible({ period: Duration.minutes(5) }),
      'Webhook events could not be handled');
    alarm('ReviewsFailed', stateMachine.metricFailed({ period: Duration.minutes(5) }), 'A review execution failed');
    alarm('IngestErrors', ingest.metricErrors({ period: Duration.minutes(5) }), 'The webhook receiver failed');

    new CfnOutput(this, 'WebhookUrl', { value: `${api.apiEndpoint}/webhook`, description: 'GitHub App webhook URL' });
    new CfnOutput(this, 'StateMachineArn', { value: stateMachine.stateMachineArn });
    new CfnOutput(this, 'TableName', { value: table.tableName });
    new CfnOutput(this, 'EvidenceBucketName', { value: evidence.bucketName });
  }
}
