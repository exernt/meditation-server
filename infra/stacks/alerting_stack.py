from __future__ import annotations

from aws_cdk import Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from constructs import Construct

from infra.config import EnvironmentConfig


class AlertingStack(Stack):
    """Shared operational alert topic for workload alarm state changes."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: EnvironmentConfig,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.topic = sns.Topic(
            self,
            "OperationalAlerts",
            topic_name=f"{config.prefix}-operational-alerts",
            display_name=f"{config.prefix} operational alerts",
        )
        self.topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyInsecureTransport",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["sns:Publish"],
                resources=[self.topic.topic_arn],
                conditions={"Bool": {"aws:SecureTransport": "false"}},
            )
        )
        self.topic.add_subscription(
            subscriptions.EmailSubscription(config.operational_alert_email)
        )
