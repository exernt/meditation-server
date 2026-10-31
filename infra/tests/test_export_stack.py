from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import assertions
from aws_cdk import aws_lambda as lambda_

from infra.config import EnvironmentConfig
from infra.stacks.alerting_stack import AlertingStack
from infra.stacks.data_stack import DataStack
from infra.stacks.export_stack import ExportStack


def test_export_stack_has_private_expiring_storage_and_worker(monkeypatch) -> None:
    app = cdk.App(
        context={
            "stage": "dev",
            "githubRepository": "example/meditation-server",
            "databaseClusterArn": (
                "arn:aws:rds:ap-southeast-2:111111111111:cluster:wm-dev-aurora-postgres"
            ),
            "workerMaxConcurrency": 2,
            "monthlyBudgetUsd": 20,
            "budgetAlertEmail": "ops@example.com",
            "operationalAlertEmail": "alerts@example.com",
            "googleMapsSecretArn": "arn:aws:secretsmanager:ap-southeast-2:111111111111:secret:google-AbC123",
            "openAiSecretArn": "arn:aws:secretsmanager:ap-southeast-2:111111111111:secret:openai-XyZ789",
            "passwordResetFromEmail": "no-reply@example.com",
            "passwordResetLinkBase": "https://api.example.com/auth/password-reset/open",
            "emailVerificationLinkBase": "https://api.example.com/auth/email-verification/open",
            "responseExportLinkBase": "https://api.example.com/response-exports",
        }
    )
    config = EnvironmentConfig.from_app(app)
    monkeypatch.setattr(
        lambda_.Code,
        "from_asset",
        staticmethod(
            lambda *_args, **_kwargs: lambda_.Code.from_inline(
                "def handler(event, context): return {}"
            )
        ),
    )
    alerting = AlertingStack(app, "Alerting", config=config)
    data = DataStack(app, "Data", config=config)
    exports = ExportStack(
        app,
        "Exports",
        config=config,
        data_stack=data,
        alert_topic=alerting.topic,
    )
    template = assertions.Template.from_stack(exports)

    template.resource_count_is("AWS::S3::Bucket", 1)
    template.resource_count_is("AWS::Scheduler::Schedule", 1)
    template.resource_count_is("AWS::CloudWatch::Alarm", 3)
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
                                "Id": "ExpireResponseExportsAfterSevenDays",
                                "Prefix": "response-exports/",
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
        "AWS::Lambda::Function",
        {
            "Handler": "meditation_server.workers.response_exports.handler",
            "LoggingConfig": {
                "ApplicationLogLevel": "INFO",
                "LogFormat": "JSON",
                "SystemLogLevel": "INFO",
            },
            "Timeout": 300,
            "Environment": {
                "Variables": assertions.Match.object_like(
                    {
                        "RESPONSE_EXPORT_BUCKET": assertions.Match.any_value(),
                        "RESPONSE_EXPORT_LINK_BASE": "https://api.example.com/response-exports",
                    }
                )
            },
        },
    )
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "meditation_server.workers.export_maintenance.handler",
            "LoggingConfig": {
                "ApplicationLogLevel": "INFO",
                "LogFormat": "JSON",
                "SystemLogLevel": "INFO",
            },
        },
    )
