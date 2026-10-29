from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk import assertions
from aws_cdk import aws_lambda as lambda_

from infra.config import EnvironmentConfig
from infra.stacks.api_stack import ApiStack
from infra.stacks.data_stack import DataStack
from infra.stacks.export_stack import ExportStack
from infra.stacks.generation_stack import GenerationStack


def _synthesized_log_groups(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> list[dict[str, object]]:
    app = cdk.App(
        context={
            "stage": stage,
            "githubRepository": "example/meditation-server",
            "databaseClusterArn": (
                "arn:aws:rds:ap-southeast-2:111111111111:"
                f"cluster:wm-{stage}-aurora-postgres"
            ),
            "workerMaxConcurrency": 2,
            "monthlyBudgetUsd": 20,
            "budgetAlertEmail": "ops@example.com",
            "googleMapsSecretArn": (
                "arn:aws:secretsmanager:ap-southeast-2:111111111111:"
                "secret:google-AbC123"
            ),
            "openAiSecretArn": (
                "arn:aws:secretsmanager:ap-southeast-2:111111111111:"
                "secret:openai-XyZ789"
            ),
            "passwordResetFromEmail": "no-reply@example.com",
            "passwordResetLinkBase": (
                "https://api.example.com/auth/password-reset/open"
            ),
            "emailVerificationLinkBase": (
                "https://api.example.com/auth/email-verification/open"
            ),
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
    data = DataStack(app, f"Data-{stage}", config=config)
    generation = GenerationStack(
        app,
        f"Generation-{stage}",
        config=config,
        data_stack=data,
    )
    exports = ExportStack(
        app,
        f"Exports-{stage}",
        config=config,
        data_stack=data,
    )
    api = ApiStack(
        app,
        f"Api-{stage}",
        config=config,
        data_stack=data,
        generation_stack=generation,
        export_stack=exports,
    )

    log_groups: list[dict[str, object]] = []
    for stack in (generation, exports, api):
        resources = assertions.Template.from_stack(stack).to_json()["Resources"]
        log_groups.extend(
            resource
            for resource in resources.values()
            if resource["Type"] == "AWS::Logs::LogGroup"
        )
    return log_groups


@pytest.mark.parametrize(
    ("stage", "expected_policy"),
    [("dev", "Delete"), ("prod", "Retain")],
)
def test_log_groups_follow_environment_removal_policy(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    expected_policy: str,
) -> None:
    log_groups = _synthesized_log_groups(monkeypatch, stage)

    assert len(log_groups) == 6
    assert {resource["DeletionPolicy"] for resource in log_groups} == {
        expected_policy
    }
    assert {resource["UpdateReplacePolicy"] for resource in log_groups} == {
        expected_policy
    }
