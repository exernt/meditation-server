from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import assertions
from aws_cdk import aws_lambda as lambda_

from infra.config import EnvironmentConfig
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
        "googleMapsSecretArn": (
            "arn:aws:secretsmanager:ap-southeast-2:111111111111:secret:google-AbC123"
        ),
        "openAiSecretArn": (
            "arn:aws:secretsmanager:ap-southeast-2:111111111111:secret:openai-XyZ789"
        ),
        "passwordResetFromEmail": "no-reply@example.com",
        "emailVerificationLinkBase": (
            "https://api.example.com/auth/email-verification/open"
        ),
    })
    config = EnvironmentConfig.from_app(app)
    monkeypatch.setattr(
        lambda_.Code,
        "from_asset",
        staticmethod(lambda *_args, **_kwargs: lambda_.Code.from_inline("def handler(event, context): return {}")),
    )
    data = DataStack(app, "Data", config=config)
    generation = GenerationStack(app, "Generation", config=config, data_stack=data)

    template = assertions.Template.from_stack(generation)
    template.resource_count_is("AWS::SQS::Queue", 3)
    template.resource_count_is("AWS::Lambda::Function", 2)
    template.resource_count_is("AWS::Scheduler::Schedule", 2)
    template.has_resource_properties(
        "AWS::Scheduler::Schedule",
        {
            "ScheduleExpression": "rate(1 minute)",
            "FlexibleTimeWindow": {"Mode": "OFF"},
        },
    )
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "meditation_server.workers.generation.handler",
            "Environment": {
                "Variables": assertions.Match.object_like(
                    {
                        "GOOGLE_MAPS_SECRET_ARN": assertions.Match.any_value(),
                        "OPENAI_SECRET_ARN": assertions.Match.any_value(),
                        "OPENAI_MODEL": "gpt-6-luna",
                        "ENABLE_NEARBY_CONTEXT": "true",
                        "SCRIPT_FALLBACK_ENABLED": "true",
                    }
                )
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
