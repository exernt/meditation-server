from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import assertions
from aws_cdk import aws_lambda as lambda_

from infra.config import EnvironmentConfig
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
    data = DataStack(app, "Data", config=config)
    generation = GenerationStack(
        app,
        "Generation",
        config=config,
        data_stack=data,
    )
    exports = ExportStack(app, "Exports", config=config, data_stack=data)
    api = ApiStack(
        app,
        "Api",
        config=config,
        data_stack=data,
        generation_stack=generation,
        export_stack=exports,
    )

    assertions.Template.from_stack(api).has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "meditation_server.api.handler.handler",
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
