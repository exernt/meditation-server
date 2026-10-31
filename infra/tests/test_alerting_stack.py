from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import assertions

from infra.config import EnvironmentConfig
from infra.stacks.alerting_stack import AlertingStack


def test_alerting_stack_has_email_subscription_and_tls_only_topic() -> None:
    app = cdk.App(
        context={
            "stage": "dev",
            "githubRepository": "example/meditation-server",
            "databaseClusterArn": (
                "arn:aws:rds:ap-southeast-2:111111111111:"
                "cluster:wm-dev-aurora-postgres"
            ),
            "workerMaxConcurrency": 2,
            "monthlyBudgetUsd": 20,
            "budgetAlertEmail": "ops@example.com",
            "operationalAlertEmail": "alerts@example.com",
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
            "responseExportLinkBase": (
                "https://api.example.com/response-exports"
            ),
        }
    )
    config = EnvironmentConfig.from_app(app)
    stack = AlertingStack(app, "Alerting", config=config)
    template = assertions.Template.from_stack(stack)

    template.has_resource_properties(
        "AWS::SNS::Topic",
        {
            "TopicName": "wm-dev-operational-alerts",
            "DisplayName": "wm-dev operational alerts",
        },
    )
    template.has_resource_properties(
        "AWS::SNS::Subscription",
        {
            "Protocol": "email",
            "Endpoint": "alerts@example.com",
        },
    )
    template.has_resource_properties(
        "AWS::SNS::TopicPolicy",
        {
            "PolicyDocument": {
                "Statement": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
                                "Action": "sns:Publish",
                                "Condition": {
                                    "Bool": {"aws:SecureTransport": "false"}
                                },
                                "Effect": "Deny",
                                "Sid": "DenyInsecureTransport",
                            }
                        )
                    ]
                )
            }
        },
    )
