from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import assertions
from aws_cdk import aws_lambda as lambda_

from infra.config import EnvironmentConfig
from infra.stacks.alerting_stack import AlertingStack
from infra.stacks.data_stack import DataStack
from infra.stacks.generation_stack import GenerationStack


def test_generation_stack_has_worker_maintenance_and_schedules(monkeypatch) -> None:
    app = cdk.App(context={
        "stage": "dev",
        "githubRepository": "example/meditation-server",
        "databaseClusterArn": (
            "arn:aws:rds:ap-southeast-2:111111111111:cluster:wm-dev-aurora-postgres"
        ),
        "workerMaxConcurrency": 2,
        "monthlyBudgetUsd": 20,
        "budgetAlertEmail": "ops@example.com",
        "operationalAlertEmail": "alerts@example.com",
        "googleMapsSecretArn": (
            "arn:aws:secretsmanager:ap-southeast-2:111111111111:secret:google-AbC123"
        ),
        "openAiSecretArn": (
            "arn:aws:secretsmanager:ap-southeast-2:111111111111:secret:openai-XyZ789"
        ),
        "passwordResetFromEmail": "no-reply@example.com",
        "passwordResetLinkBase": (
            "https://api.example.com/auth/password-reset/open"
        ),
        "emailVerificationLinkBase": (
            "https://api.example.com/auth/email-verification/open"
        ),
        "responseExportLinkBase": "https://api.example.com/response-exports",
    })
    config = EnvironmentConfig.from_app(app)
    monkeypatch.setattr(
        lambda_.Code,
        "from_asset",
        staticmethod(lambda *_args, **_kwargs: lambda_.Code.from_inline("def handler(event, context): return {}")),
    )
    alerting = AlertingStack(app, "Alerting", config=config)
    data = DataStack(app, "Data", config=config)
    generation = GenerationStack(
        app,
        "Generation",
        config=config,
        data_stack=data,
        alert_topic=alerting.topic,
    )

    template = assertions.Template.from_stack(generation)
    template.resource_count_is("AWS::SQS::Queue", 3)
    # Worker, maintenance, and the dev bucket's auto-delete custom resource.
    template.resource_count_is("AWS::Lambda::Function", 3)
    template.resource_count_is("AWS::Scheduler::Schedule", 3)
    template.resource_count_is("AWS::S3::Bucket", 1)
    template.resource_count_is("AWS::CloudWatch::Alarm", 4)
    for alarm in template.find_resources("AWS::CloudWatch::Alarm").values():
        alarm_actions = alarm["Properties"]["AlarmActions"]
        ok_actions = alarm["Properties"]["OKActions"]
        assert len(alarm_actions) == 1
        assert ok_actions == alarm_actions
        assert "Fn::ImportValue" in alarm_actions[0]
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "BucketEncryption": {
                "ServerSideEncryptionConfiguration": [
                    {
                        "ServerSideEncryptionByDefault": {
                            "SSEAlgorithm": "AES256"
                        }
                    }
                ]
            },
            "LifecycleConfiguration": {
                "Rules": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
                                "Id": "ExpireGenerationResultsAfterSevenDays",
                                "ExpirationInDays": 7,
                                "Status": "Enabled",
                            }
                        )
                    ]
                )
            },
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
        },
    )
    template.has_resource_properties(
        "AWS::Scheduler::Schedule",
        {
            "ScheduleExpression": "rate(1 minute)",
            "FlexibleTimeWindow": {"Mode": "OFF"},
        },
    )
    template.has_resource_properties(
        "AWS::Scheduler::Schedule",
        {
            "ScheduleExpression": "cron(30 3 * * ? *)",
            "ScheduleExpressionTimezone": "Australia/Sydney",
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "Target": assertions.Match.object_like(
                {"Input": '{"task": "unverified-account-cleanup"}'}
            ),
        },
    )
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "meditation_server.workers.generation.handler",
            "LoggingConfig": {
                "ApplicationLogLevel": "INFO",
                "LogFormat": "JSON",
                "SystemLogLevel": "INFO",
            },
            "Environment": {
                "Variables": assertions.Match.object_like(
                    {
                        "GOOGLE_MAPS_SECRET_ARN": assertions.Match.any_value(),
                        "OPENAI_SECRET_ARN": assertions.Match.any_value(),
                        "OPENAI_MODEL": "gpt-6-luna",
                        "ENABLE_NEARBY_CONTEXT": "true",
                        "SCRIPT_FALLBACK_ENABLED": "true",
                        "GENERATION_ARCHIVE_BUCKET": assertions.Match.any_value(),
                    }
                )
            },
        },
    )
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "meditation_server.workers.maintenance.handler",
            "LoggingConfig": {
                "ApplicationLogLevel": "INFO",
                "LogFormat": "JSON",
                "SystemLogLevel": "INFO",
            },
        },
    )
    template.has_resource_properties(
        "AWS::Scheduler::Schedule",
        {
            "ScheduleExpression": "cron(15 3 * * ? *)",
            "ScheduleExpressionTimezone": "Australia/Sydney",
            "FlexibleTimeWindow": {"Mode": "OFF"},
        },
    )
