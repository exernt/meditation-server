from __future__ import annotations

import json
from pathlib import Path

from aws_cdk import BundlingOptions, CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as event_sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_sqs as sqs
from cdk_nag import NagSuppressions
from constructs import Construct

from infra.config import EnvironmentConfig
from infra.stacks.data_stack import DataStack


class ExportStack(Stack):
    """Private response-export storage and asynchronous CSV generation."""

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
        log_removal_policy = RemovalPolicy.RETAIN if config.is_production else RemovalPolicy.DESTROY

        self.bucket = s3.Bucket(
            self,
            "ResponseExportBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            minimum_tls_version=1.2,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireResponseExportsAfterSevenDays",
                    enabled=True,
                    prefix="response-exports/",
                    expiration=Duration.days(7),
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                )
            ],
            removal_policy=log_removal_policy,
            auto_delete_objects=not config.is_production,
        )
        NagSuppressions.add_resource_suppressions(
            self.bucket,
            [
                {
                    "id": "AwsSolutions-S1",
                    "reason": (
                        "CloudTrail data events audit object reads and writes for the "
                        "response-exports prefix; a second server-access-log bucket is unnecessary."
                    ),
                }
            ],
            apply_to_children=False,
        )

        self.dead_letter_queue = sqs.Queue(
            self,
            "ResponseExportDeadLetterQueue",
            queue_name=f"{config.prefix}-response-exports-dlq",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=Duration.days(14),
        )
        self.queue = sqs.Queue(
            self,
            "ResponseExportQueue",
            queue_name=f"{config.prefix}-response-exports",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            visibility_timeout=Duration.minutes(30),
            retention_period=Duration.days(4),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=5,
                queue=self.dead_letter_queue,
            ),
        )

        project_root = Path(__file__).resolve().parents[2]

        def lambda_code():
            return lambda_.Code.from_asset(
                str(project_root),
                bundling=BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_14.bundling_image,
                    command=[
                        "bash",
                        "-c",
                        "pip install --no-cache-dir -r /asset-input/requirements-lambda.txt -t /asset-output && cp -r /asset-input/src/. /asset-output/",
                    ],
                ),
            )

        worker_role = iam.Role(
            self,
            "ResponseExportWorkerRole",
            role_name=f"{config.prefix}-response-export-worker-exec",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        worker_logs = logs.LogGroup(
            self,
            "ResponseExportWorkerLogs",
            log_group_name=f"/aws/lambda/{config.prefix}-response-export-worker",
            retention=logs.RetentionDays.THREE_MONTHS if config.is_production else logs.RetentionDays.ONE_WEEK,
            removal_policy=log_removal_policy,
        )
        worker_logs.grant_write(worker_role)
        self._grant_database(worker_role, config, data_stack)
        worker_role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteResponseExportObjects",
                actions=["s3:PutObject"],
                resources=[self.bucket.arn_for_objects("response-exports/*")],
            )
        )
        worker_role.add_to_policy(
            iam.PolicyStatement(
                sid="SendResponseExportEmail",
                actions=["ses:SendEmail"],
                resources=["*"],
                conditions={"StringEquals": {"aws:RequestedRegion": config.region}},
            )
        )
        NagSuppressions.add_resource_suppressions(
            worker_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "Each export has a unique key below response-exports; PutObject is "
                        "the only S3 action, and SES SendEmail has no resource-level ARN."
                    ),
                }
            ],
            apply_to_children=True,
        )
        self.worker = lambda_.Function(
            self,
            "ResponseExportWorker",
            function_name=f"{config.prefix}-response-export-worker",
            runtime=lambda_.Runtime.PYTHON_3_14,
            handler="meditation_server.workers.response_exports.handler",
            code=lambda_code(),
            role=worker_role,
            log_group=worker_logs,
            logging_format=lambda_.LoggingFormat.JSON,
            application_log_level_v2=lambda_.ApplicationLogLevel.INFO,
            system_log_level_v2=lambda_.SystemLogLevel.INFO,
            timeout=Duration.minutes(5),
            memory_size=1024,
            environment={
                "STAGE": config.stage,
                "DATABASE_RESOURCE_ARN": config.database_cluster_arn,
                "DATABASE_SECRET_ARN": data_stack.application_secret.secret_arn,
                "DATABASE_NAME": config.database_name,
                "RESPONSE_EXPORT_BUCKET": self.bucket.bucket_name,
                "RESPONSE_EXPORT_LINK_BASE": config.response_export_link_base,
                "EMAIL_BACKEND": "ses",
                "PASSWORD_RESET_FROM_EMAIL": config.password_reset_from_email,
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

        maintenance_role = iam.Role(
            self,
            "ResponseExportMaintenanceRole",
            role_name=f"{config.prefix}-response-export-maintenance-exec",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        maintenance_logs = logs.LogGroup(
            self,
            "ResponseExportMaintenanceLogs",
            log_group_name=f"/aws/lambda/{config.prefix}-response-export-maintenance",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=log_removal_policy,
        )
        maintenance_logs.grant_write(maintenance_role)
        self._grant_database(maintenance_role, config, data_stack)
        self.queue.grant_send_messages(maintenance_role)
        maintenance = lambda_.Function(
            self,
            "ResponseExportMaintenance",
            function_name=f"{config.prefix}-response-export-maintenance",
            runtime=lambda_.Runtime.PYTHON_3_14,
            handler="meditation_server.workers.export_maintenance.handler",
            code=lambda_code(),
            role=maintenance_role,
            log_group=maintenance_logs,
            logging_format=lambda_.LoggingFormat.JSON,
            application_log_level_v2=lambda_.ApplicationLogLevel.INFO,
            system_log_level_v2=lambda_.SystemLogLevel.INFO,
            timeout=Duration.minutes(1),
            memory_size=512,
            environment={
                "STAGE": config.stage,
                "DATABASE_RESOURCE_ARN": config.database_cluster_arn,
                "DATABASE_SECRET_ARN": data_stack.application_secret.secret_arn,
                "DATABASE_NAME": config.database_name,
                "RESPONSE_EXPORT_QUEUE_URL": self.queue.queue_url,
            },
        )

        scheduler_role = iam.Role(
            self,
            "ResponseExportSchedulerRole",
            role_name=f"{config.prefix}-response-export-scheduler",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        scheduler_dlq = sqs.Queue(
            self,
            "ResponseExportSchedulerDeadLetterQueue",
            queue_name=f"{config.prefix}-response-export-maintenance-dlq",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=Duration.days(14),
        )
        NagSuppressions.add_resource_suppressions(
            scheduler_dlq,
            [{"id": "AwsSolutions-SQS3", "reason": "Terminal DLQ for failed export maintenance invocations."}],
            apply_to_children=False,
        )
        maintenance.grant_invoke_latest_version(scheduler_role)
        scheduler_dlq.grant_send_messages(scheduler_role)
        scheduler.CfnSchedule(
            self,
            "ResponseExportOutboxRecoverySchedule",
            name=f"{config.prefix}-response-export-outbox-recovery",
            description="Retry unpublished response-export outbox records every minute.",
            schedule_expression="rate(1 minute)",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=maintenance.function_arn,
                role_arn=scheduler_role.role_arn,
                input=json.dumps({"task": "outbox-recovery"}),
                dead_letter_config=scheduler.CfnSchedule.DeadLetterConfigProperty(
                    arn=scheduler_dlq.queue_arn
                ),
                retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(
                    maximum_event_age_in_seconds=3600,
                    maximum_retry_attempts=3,
                ),
            ),
        )

        cloudwatch.Alarm(
            self,
            "ResponseExportDeadLetterQueueAlarm",
            alarm_name=f"{config.prefix}-response-exports-dlq-not-empty",
            metric=self.dead_letter_queue.metric_approximate_number_of_messages_visible(),
            threshold=1,
            evaluation_periods=1,
        )
        cloudwatch.Alarm(
            self,
            "ResponseExportOldestMessageAlarm",
            alarm_name=f"{config.prefix}-response-export-message-age",
            metric=self.queue.metric_approximate_age_of_oldest_message(),
            threshold=300,
            evaluation_periods=2,
        )
        cloudwatch.Alarm(
            self,
            "ResponseExportWorkerErrorsAlarm",
            alarm_name=f"{config.prefix}-response-export-worker-errors",
            metric=self.worker.metric_errors(),
            threshold=1,
            evaluation_periods=1,
        )

        CfnOutput(
            self,
            "ResponseExportBucketName",
            value=self.bucket.bucket_name,
            description="Private CSV response exports; objects expire after seven days.",
        )

    @staticmethod
    def _grant_database(role: iam.Role, config: EnvironmentConfig, data_stack: DataStack) -> None:
        role.add_to_policy(
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
        data_stack.application_secret.grant_read(role)
