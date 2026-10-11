import { Duration } from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import { Construct } from 'constructs';
import { NAMES } from './names';

export interface ReviewSteps {
  readonly context: lambda.IFunction;
  readonly policy: lambda.IFunction;
  readonly waitCi: lambda.IFunction;
  readonly verify: lambda.IFunction;
  readonly review: lambda.IFunction;
  readonly validate: lambda.IFunction;
  readonly decide: lambda.IFunction;
  readonly publish: lambda.IFunction;
  readonly publishFailure: lambda.IFunction;
}

export interface ReviewStateMachineProps {
  readonly steps: ReviewSteps;
  /** How long a review waits for CI before a person has to look. */
  readonly ciWait: Duration;
}

/**
 * One execution reviews one commit:
 * context -> policy -> wait for CI -> check results -> AI review -> validate -> decide -> publish.
 * A gate that already decides skips straight to `Decide`, so the AI is only called when every
 * cheaper check passed. Any failure publishes "needs human review" instead of leaving the PR stuck.
 */
export class ReviewStateMachine extends Construct {
  readonly stateMachine: sfn.StateMachine;

  constructor(scope: Construct, id: string, props: ReviewStateMachineProps) {
    super(scope, id);
    const { steps } = props;

    const input = (withToken = false) =>
      sfn.TaskInput.fromObject({
        state: sfn.JsonPath.entirePayload,
        execution: { name: sfn.JsonPath.executionName, start_time: sfn.JsonPath.executionStartTime },
        ...(withToken ? { token: sfn.JsonPath.taskToken } : {}),
      });
    const transient = (task: tasks.LambdaInvoke) =>
      task.addRetry({
        errors: ['TransientGitHubError', 'ThrottlingException', 'Lambda.TooManyRequestsException'],
        interval: Duration.seconds(5),
        maxAttempts: 3,
        backoffRate: 2,
      });
    const step = (stateId: string, fn: lambda.IFunction, resultPath: string) =>
      transient(new tasks.LambdaInvoke(this, stateId, { lambdaFunction: fn, payload: input(), payloadResponseOnly: true, resultPath }));

    const publishFailure = step('PublishFailure', steps.publishFailure, '$.failure_published').next(
      new sfn.Fail(this, 'ReviewFailed', { error: 'ReviewFailed', cause: 'A review step failed; see the execution history.' }),
    );
    const onError = { errors: ['States.ALL'], resultPath: '$.error' };

    const context = step('CollectContext', steps.context, '$.ctx');
    const policy = step('CheckPolicy', steps.policy, '$.policy');
    const verify = step('CheckResults', steps.verify, '$.checks');
    const review = step('AIReview', steps.review, '$.review');
    const validate = step('ValidateAnswer', steps.validate, '$.validation');
    const decide = step('Decide', steps.decide, '$.decision');
    const publish = step('Publish', steps.publish, '$.published');
    const waitCi = transient(
      new tasks.LambdaInvoke(this, 'WaitForCI', {
        lambdaFunction: steps.waitCi,
        integrationPattern: sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
        payload: input(true),
        resultPath: '$.ci',
        taskTimeout: sfn.Timeout.duration(props.ciWait),
      }),
    );
    const ciTimedOut = new sfn.Pass(this, 'CITimedOut', {
      result: sfn.Result.fromObject({ complete: false, timed_out: true, workflows: {} }),
      resultPath: '$.ci',
    });
    waitCi.addCatch(ciTimedOut, { errors: [sfn.Errors.TIMEOUT], resultPath: '$.ci_error' });
    for (const task of [context, policy, waitCi, verify, review, validate, decide, publish]) {
      task.addCatch(publishFailure, onError);
    }

    decide.next(publish).next(new sfn.Succeed(this, 'Published'));
    ciTimedOut.next(verify);
    const afterReview = new sfn.Choice(this, 'AIAnswered')
      .when(sfn.Condition.stringEquals('$.review.status', 'ok'), validate.next(decide))
      .otherwise(decide);
    const afterChecks = new sfn.Choice(this, 'ChecksBlock')
      .when(sfn.Condition.booleanEquals('$.checks.blocking', true), decide)
      .otherwise(review.next(afterReview));
    const afterPolicy = new sfn.Choice(this, 'PolicyBlocks')
      .when(sfn.Condition.booleanEquals('$.policy.blocking', true), decide)
      .otherwise(waitCi.next(verify).next(afterChecks));
    const definition = context.next(
      new sfn.Choice(this, 'Skip')
        .when(sfn.Condition.booleanEquals('$.ctx.skip', true), new sfn.Succeed(this, 'Skipped'))
        .otherwise(policy.next(afterPolicy)),
    );

    this.stateMachine = new sfn.StateMachine(this, 'Review', {
      stateMachineName: NAMES.stateMachine,
      stateMachineType: sfn.StateMachineType.STANDARD,
      definitionBody: sfn.DefinitionBody.fromChainable(definition),
      timeout: Duration.hours(2),
    });
  }
}
