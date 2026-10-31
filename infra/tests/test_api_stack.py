from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import assertions
from aws_cdk import aws_lambda as lambda_

from infra.config import EnvironmentConfig
from infra.stacks.alerting_stack import AlertingStack
from infra.stacks.api_stack import ApiStack
from infra.stacks.data_stack import DataStack
from infra.stacks.export_stack import ExportStack
from infra.stacks.generation_stack import GenerationStack


def test_api_lambda_receives_password_reset_link_base(monkeypatch) -> None:
    password_reset_link = (
        "https://example.execute-api.ap-southeast-2.amazonaws.com/"
        "dev/auth/password-reset/open"
    )
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
        "passwordResetLinkBase": password_reset_link,
        "emailVerificationLinkBase": (
            "https://api.example.com/auth/email-verification/open"
        ),
        "responseExportLinkBase": "https://api.example.com/response-exports",
    })
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
    generation = GenerationStack(
        app,
        "Generation",
        config=config,
        data_stack=data,
        alert_topic=alerting.topic,
    )
    exports = ExportStack(
        app,
        "Exports",
        config=config,
        data_stack=data,
        alert_topic=alerting.topic,
    )
    api = ApiStack(
        app,
        "Api",
        config=config,
        data_stack=data,
        generation_stack=generation,
        export_stack=exports,
        alert_topic=alerting.topic,
    )

    template = assertions.Template.from_stack(api)
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "meditation_server.api.handler.handler",
            "Timeout": 30,
            "LoggingConfig": {
                "ApplicationLogLevel": "INFO",
                "LogFormat": "JSON",
                "SystemLogLevel": "INFO",
            },
            "Environment": {
                "Variables": assertions.Match.object_like(
                    {"PASSWORD_RESET_LINK_BASE": password_reset_link}
                )
            },
        },
    )
    template.resource_count_is("AWS::CloudWatch::Alarm", 5)
    expected_alarms = {
        "wm-dev-api-5xx-rate": {
            "MetricName": "5XXError",
            "Namespace": "AWS/ApiGateway",
            "Statistic": "Average",
            "Threshold": 0.01,
            "EvaluationPeriods": 3,
            "DatapointsToAlarm": 2,
        },
        "wm-dev-api-5xx-count": {
            "MetricName": "5XXError",
            "Namespace": "AWS/ApiGateway",
            "Statistic": "Sum",
            "Threshold": 3,
            "EvaluationPeriods": 1,
        },
        "wm-dev-api-lambda-errors": {
            "MetricName": "Errors",
            "Namespace": "AWS/Lambda",
            "Statistic": "Sum",
            "Threshold": 1,
            "EvaluationPeriods": 1,
        },
        "wm-dev-api-lambda-duration": {
            "MetricName": "Duration",
            "Namespace": "AWS/Lambda",
            "Statistic": "Maximum",
            "Threshold": 25_000,
            "EvaluationPeriods": 3,
            "DatapointsToAlarm": 2,
        },
        "wm-dev-api-lambda-throttles": {
            "MetricName": "Throttles",
            "Namespace": "AWS/Lambda",
            "Statistic": "Sum",
            "Threshold": 1,
            "EvaluationPeriods": 1,
        },
    }
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    assert {alarm["Properties"]["AlarmName"] for alarm in alarms.values()} == set(
        expected_alarms
    )
    for alarm in alarms.values():
        properties = alarm["Properties"]
        expected = expected_alarms[properties["AlarmName"]]
        for key, value in expected.items():
            assert properties[key] == value
        assert properties["Period"] == 300
        assert properties["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
        assert properties["TreatMissingData"] == "notBreaching"
        assert len(properties["AlarmActions"]) == 1
        assert properties["OKActions"] == properties["AlarmActions"]
        assert "Fn::ImportValue" in properties["AlarmActions"][0]
    template.has_resource_properties(
        "AWS::ApiGateway::Method",
        {
            "Integration": assertions.Match.object_like(
                {"TimeoutInMillis": 29_000}
            )
        },
    )
    template.has_output(
        "ApiBaseUrl",
        {
            "Description": "Base URL used by clients and post-deployment smoke tests.",
        },
    )
