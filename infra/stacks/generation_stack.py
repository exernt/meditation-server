from __future__ import annotations

from pathlib import Path

from aws_cdk import Duration, Stack
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as event_sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from infra.config import EnvironmentConfig
from infra.stacks.data_stack import DataStack


class GenerationStack(Stack):
    """Asynchronous generation queue, worker, DLQ, and operational alarms."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: EnvironmentConfig,
        data_stack: DataStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.dead_letter_queue = sqs.Queue(
            self,
            "DeadLetterQueue",
            queue_name=f"{config.prefix}-generation-dlq",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=Duration.days(14),
        )
        self.queue = sqs.Queue(
            self,
            "GenerationQueue",
            queue_name=f"{config.prefix}-generation",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            visibility_timeout=Duration.minutes(30),
            retention_period=Duration.days(4),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=5,
                queue=self.dead_letter_queue,
            ),
        )

        worker_role = iam.Role(
            self,
            "WorkerRole",
            role_name=f"{config.prefix}-worker-exec",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        worker_logs = logs.LogGroup(
            self,
            "WorkerLogs",
            log_group_name=f"/aws/lambda/{config.prefix}-generation-worker",
            retention=logs.RetentionDays.TWO_WEEKS,
        )
        worker_logs.grant_write(worker_role)
        worker_role.add_to_policy(
            iam.PolicyStatement(
                sid="ExecuteAuroraDataApiStatements",
                actions=[
                    "rds-data:BatchExecuteStatement",
                    "rds-data:BeginTransaction",
                    "rds-data:CommitTransaction",
                    "rds-data:ExecuteStatement",
                    "rds-data:RollbackTransaction",
                ],
                resources=[config.database_cluster_arn],
            )
        )
        data_stack.application_secret.grant_read(worker_role)
        openai_secret = secretsmanager.Secret.from_secret_complete_arn(
            self, "OpenAiSecret", config.openai_secret_arn
        )
        openai_secret.grant_read(worker_role)

        source_root = Path(__file__).resolve().parents[2] / "src"
        self.worker = lambda_.Function(
            self,
            "Worker",
            function_name=f"{config.prefix}-generation-worker",
            runtime=lambda_.Runtime.PYTHON_3_14,
            handler="meditation_server.workers.generation.handler",
            code=lambda_.Code.from_asset(str(source_root)),
            role=worker_role,
            log_group=worker_logs,
            timeout=Duration.minutes(5),
            memory_size=1024,
            environment={
                "STAGE": config.stage,
                "DATABASE_RESOURCE_ARN": config.database_cluster_arn,
                "DATABASE_SECRET_ARN": data_stack.application_secret.secret_arn,
                "DATABASE_NAME": config.database_name,
                "OPENAI_SECRET_ARN": openai_secret.secret_arn,
            },
        )
        self.queue.grant_consume_messages(worker_role)
        self.worker.add_event_source(
            event_sources.SqsEventSource(
                self.queue,
                batch_size=1,
                report_batch_item_failures=True,
                max_concurrency=config.worker_max_concurrency,
            )
        )

        cloudwatch.Alarm(
            self,
            "DeadLetterQueueAlarm",
            alarm_name=f"{config.prefix}-generation-dlq-not-empty",
            metric=self.dead_letter_queue.metric_approximate_number_of_messages_visible(),
            threshold=1,
            evaluation_periods=1,
        )
        cloudwatch.Alarm(
            self,
            "OldestMessageAlarm",
            alarm_name=f"{config.prefix}-generation-message-age",
            metric=self.queue.metric_approximate_age_of_oldest_message(),
            threshold=300,
            evaluation_periods=2,
        )

