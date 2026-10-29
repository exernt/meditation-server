from __future__ import annotations

import json
from pathlib import Path

from aws_cdk import BundlingOptions, CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as event_sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from cdk_nag import NagSuppressions
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
        log_removal_policy = (
            RemovalPolicy.RETAIN
            if config.is_production
            else RemovalPolicy.DESTROY
        )

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

        self.script_archive: s3.Bucket | None = None
        if not config.is_production:
            self.script_archive = s3.Bucket(
                self,
                "ScriptArchive",
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                encryption=s3.BucketEncryption.S3_MANAGED,
                enforce_ssl=True,
                lifecycle_rules=[
                    s3.LifecycleRule(
                        id="ExpireGenerationResultsAfterSevenDays",
                        enabled=True,
                        expiration=Duration.days(7),
                        abort_incomplete_multipart_upload_after=Duration.days(1),
                    )
                ],
                removal_policy=RemovalPolicy.DESTROY,
                auto_delete_objects=True,
            )
            NagSuppressions.add_resource_suppressions(
                self.script_archive,
                [
                    {
                        "id": "AwsSolutions-S1",
                        "reason": (
                            "This short-lived development-only bucket is written by one "
                            "Lambda role; generation outcomes and archive keys are logged "
                            "to the worker's seven-day CloudWatch log group."
                        ),
                    }
                ],
                apply_to_children=False,
            )
            CfnOutput(
                self,
                "ScriptArchiveBucketName",
                value=self.script_archive.bucket_name,
                description="Private dev generation-result archive; objects expire after seven days.",
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
            retention=(
                logs.RetentionDays.THREE_MONTHS
                if config.is_production
                else logs.RetentionDays.ONE_WEEK
            ),
            removal_policy=log_removal_policy,
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
        google_secret = secretsmanager.Secret.from_secret_complete_arn(
            self, "GoogleMapsSecret", config.google_maps_secret_arn
        )
        google_secret.grant_read(worker_role)
        openai_secret = secretsmanager.Secret.from_secret_complete_arn(
            self, "OpenAiSecret", config.openai_secret_arn
        )
        openai_secret.grant_read(worker_role)
        if self.script_archive is not None:
            worker_role.add_to_policy(
                iam.PolicyStatement(
                    sid="ArchiveGenerationResults",
                    actions=["s3:PutObject"],
                    resources=[
                        self.script_archive.arn_for_objects("generation-results/*")
                    ],
                )
            )
            NagSuppressions.add_resource_suppressions(
                worker_role,
                [
                    {
                        "id": "AwsSolutions-IAM5",
                        "reason": (
                            "Each generation job has a distinct object key below the "
                            "generation-results prefix; PutObject is the only allowed "
                            "S3 action and bucket access is not granted."
                        ),
                        "appliesTo": [
                            "Resource::<ScriptArchive108F42B0.Arn>/generation-results/*"
                        ],
                    }
                ],
                apply_to_children=True,
            )

        project_root = Path(__file__).resolve().parents[2]
        def lambda_code(requirements_file: str):
            return lambda_.Code.from_asset(
                str(project_root),
                bundling=BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_14.bundling_image,
                    command=[
                        "bash",
                        "-c",
                        f"pip install --no-cache-dir -r /asset-input/{requirements_file} -t /asset-output && cp -r /asset-input/src/. /asset-output/",
                    ],
                ),
            )
        worker_environment = {
            "STAGE": config.stage,
            "DATABASE_RESOURCE_ARN": config.database_cluster_arn,
            "DATABASE_SECRET_ARN": data_stack.application_secret.secret_arn,
            "DATABASE_NAME": config.database_name,
            "GOOGLE_MAPS_SECRET_ARN": google_secret.secret_arn,
            "OPENAI_SECRET_ARN": openai_secret.secret_arn,
            "OPENAI_MODEL": "gpt-6-luna",
            "ENABLE_NEARBY_CONTEXT": "true",
            "SCRIPT_FALLBACK_ENABLED": "true",
        }
        if self.script_archive is not None:
            worker_environment["GENERATION_ARCHIVE_BUCKET"] = (
                self.script_archive.bucket_name
            )

        self.worker = lambda_.Function(
            self,
            "Worker",
            function_name=f"{config.prefix}-generation-worker",
            runtime=lambda_.Runtime.PYTHON_3_14,
            handler="meditation_server.workers.generation.handler",
            code=lambda_code("requirements-worker.txt"),
            role=worker_role,
            log_group=worker_logs,
            timeout=Duration.minutes(5),
            memory_size=1024,
            environment=worker_environment,
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

        maintenance_role = iam.Role(
            self,
            "MaintenanceRole",
            role_name=f"{config.prefix}-generation-maintenance-exec",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        maintenance_logs = logs.LogGroup(
            self,
            "MaintenanceLogs",
            log_group_name=f"/aws/lambda/{config.prefix}-generation-maintenance",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=log_removal_policy,
        )
        maintenance_logs.grant_write(maintenance_role)
        maintenance_role.add_to_policy(
            iam.PolicyStatement(
                sid="ExecuteAuroraDataApiStatements",
                actions=[
                    "rds-data:BeginTransaction",
                    "rds-data:CommitTransaction",
                    "rds-data:ExecuteStatement",
                    "rds-data:RollbackTransaction",
                ],
                resources=[config.database_cluster_arn],
            )
        )
        data_stack.application_secret.grant_read(maintenance_role)
        self.queue.grant_send_messages(maintenance_role)
        self.maintenance = lambda_.Function(
            self,
            "Maintenance",
            function_name=f"{config.prefix}-generation-maintenance",
            runtime=lambda_.Runtime.PYTHON_3_14,
            handler="meditation_server.workers.maintenance.handler",
            code=lambda_code("requirements-lambda.txt"),
            role=maintenance_role,
            log_group=maintenance_logs,
            timeout=Duration.minutes(1),
            memory_size=512,
            environment={
                "STAGE": config.stage,
                "DATABASE_RESOURCE_ARN": config.database_cluster_arn,
                "DATABASE_SECRET_ARN": data_stack.application_secret.secret_arn,
                "DATABASE_NAME": config.database_name,
                "GENERATION_QUEUE_URL": self.queue.queue_url,
            },
        )
        scheduler_role = iam.Role(
            self,
            "SchedulerRole",
            role_name=f"{config.prefix}-generation-scheduler",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        scheduler_dlq = sqs.Queue(
            self,
            "SchedulerDeadLetterQueue",
            queue_name=f"{config.prefix}-generation-maintenance-dlq",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=Duration.days(14),
        )
        NagSuppressions.add_resource_suppressions(
            scheduler_dlq,
            [
                {
                    "id": "AwsSolutions-SQS3",
                    "reason": "This is the terminal dead-letter queue for failed EventBridge Scheduler invocations.",
                }
            ],
            apply_to_children=False,
        )
        self.maintenance.grant_invoke_latest_version(scheduler_role)
        scheduler_dlq.grant_send_messages(scheduler_role)
        scheduler_target_options = {
            "dead_letter_config": scheduler.CfnSchedule.DeadLetterConfigProperty(
                arn=scheduler_dlq.queue_arn
            ),
            "retry_policy": scheduler.CfnSchedule.RetryPolicyProperty(
                maximum_event_age_in_seconds=3600,
                maximum_retry_attempts=3,
            ),
        }
        scheduler.CfnSchedule(
            self,
            "OutboxRecoverySchedule",
            name=f"{config.prefix}-generation-outbox-recovery",
            description="Retry unpublished generation outbox records every minute.",
            schedule_expression="rate(1 minute)",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=self.maintenance.function_arn,
                role_arn=scheduler_role.role_arn,
                input=json.dumps({"task": "outbox-recovery"}),
                **scheduler_target_options,
            ),
        )
        scheduler.CfnSchedule(
            self,
            "RetentionCleanupSchedule",
            name=f"{config.prefix}-generation-retention-cleanup",
            description="Delete expired generation jobs and delivered outbox records.",
            schedule_expression="cron(15 3 * * ? *)",
            schedule_expression_timezone="Australia/Sydney",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=self.maintenance.function_arn,
                role_arn=scheduler_role.role_arn,
                input=json.dumps({"task": "retention-cleanup"}),
                **scheduler_target_options,
            ),
        )
        scheduler.CfnSchedule(
            self,
            "UnverifiedAccountCleanupSchedule",
            name=f"{config.prefix}-unverified-account-cleanup",
            description="Delete application accounts left unverified for seven days.",
            schedule_expression="cron(30 3 * * ? *)",
            schedule_expression_timezone="Australia/Sydney",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=self.maintenance.function_arn,
                role_arn=scheduler_role.role_arn,
                input=json.dumps({"task": "unverified-account-cleanup"}),
                **scheduler_target_options,
            ),
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
            "SchedulerDeadLetterQueueAlarm",
            alarm_name=f"{config.prefix}-generation-maintenance-dlq-not-empty",
            metric=scheduler_dlq.metric_approximate_number_of_messages_visible(),
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
        cloudwatch.Alarm(
            self,
            "MaintenanceErrorsAlarm",
            alarm_name=f"{config.prefix}-generation-maintenance-errors",
            metric=self.maintenance.metric_errors(),
            threshold=1,
            evaluation_periods=1,
        )

