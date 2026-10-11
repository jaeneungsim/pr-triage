#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { CoreStack } from '../lib/core-stack';
import { PreviewPlatformStack } from '../lib/preview-platform-stack';

const app = new cdk.App();
const ctx = <T>(key: string): T => app.node.getContext(key) as T;
const env = { account: process.env.CDK_DEFAULT_ACCOUNT, region: 'ap-southeast-2' };

const platform = new PreviewPlatformStack(app, 'PrTriagePreviewPlatform', {
  env,
  stackName: 'pr-triage-preview-platform',
  description: 'Isolation for demo app previews: GitHub OIDC roles, a CloudFormation role and a permissions boundary',
  githubOwner: ctx<string>('githubOwner'),
  githubOidcProviderArn: ctx<string>('githubOidcProviderArn') || undefined,
});

new CoreStack(app, 'PrTriageCore', {
  env,
  stackName: 'pr-triage-core',
  description: 'AI-first PR triage: webhook, review workflow, AI review on Bedrock, decision in code',
  githubOwner: ctx<string>('githubOwner'),
  managedRepos: ctx<string[]>('managedRepos'),
  ticketRepo: ctx<string>('ticketRepo'),
  webPreviewDomain: platform.webPreviewDomain,
  webPreviewBucketName: platform.webPreviewBucket.bucketName,
  level: ctx<string>('level'),
  mode: ctx<string>('mode'),
  modelId: ctx<string>('modelId'),
  modelInputUsdPerMTok: ctx<number>('modelInputUsdPerMTok'),
  modelOutputUsdPerMTok: ctx<number>('modelOutputUsdPerMTok'),
  allowedAuthors: ctx<string[]>('allowedAuthors'),
  dailyReviews: ctx<number>('dailyReviews'),
  monthlyAiUsd: ctx<number>('monthlyAiUsd'),
  budgetWarnUsd: ctx<number>('budgetWarnUsd'),
  budgetStopUsd: ctx<number>('budgetStopUsd'),
  alertEmail: ctx<string>('alertEmail') || undefined,
});

cdk.Tags.of(app).add('project', 'pr-triage');
