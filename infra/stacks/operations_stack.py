from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_cloudtrail as cloudtrail
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from aws_cdk import aws_iam as iam
from cdk_nag import NagSuppressions
from constructs import Construct

from infra.config import EnvironmentConfig


class OperationsStack(Stack):
    """Budget alerts and durable management-event audit trail."""

    def __init__(self, scope: Construct, construct_id: str, *, config: EnvironmentConfig, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        alerts = sns.Topic(self, "CostAlerts", topic_name=f"{config.prefix}-cost-alerts")
        alerts.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyInsecureTransport",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["sns:Publish"],
                resources=[alerts.topic_arn],
                conditions={"Bool": {"aws:SecureTransport": "false"}},
            )
        )
        alerts.add_subscription(subscriptions.EmailSubscription(config.budget_alert_email))

        notifications = []
        for threshold in (50, 80, 100):
            notifications.append(
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        comparison_operator="GREATER_THAN",
                        notification_type="ACTUAL",
                        threshold=threshold,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            subscription_type="SNS",
                            address=alerts.topic_arn,
                        )
                    ],
                )
            )
        notifications.append(
            budgets.CfnBudget.NotificationWithSubscribersProperty(
                notification=budgets.CfnBudget.NotificationProperty(
                    comparison_operator="GREATER_THAN",
                    notification_type="FORECASTED",
                    threshold=100,
                    threshold_type="PERCENTAGE",
                ),
                subscribers=[
                    budgets.CfnBudget.SubscriberProperty(
                        subscription_type="SNS",
                        address=alerts.topic_arn,
                    )
                ],
            )
        )
        budgets.CfnBudget(
            self,
            "MonthlyBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name=f"{config.prefix}-monthly",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=config.monthly_budget_usd,
                    unit="USD",
                ),
                cost_filters={"TagKeyValue": [f"user:Environment${config.stage}"]},
            ),
            notifications_with_subscribers=notifications,
        )

        trail_bucket = s3.Bucket(
            self,
            "CloudTrailBucket",
            bucket_name=None,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            versioned=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(365))],
            removal_policy=RemovalPolicy.RETAIN,
        )
        NagSuppressions.add_resource_suppressions(
            trail_bucket,
            [
                {
                    "id": "AwsSolutions-S1",
                    "reason": "This bucket receives CloudTrail logs only; log-file validation is enabled and a second access-log bucket would add recursive storage and policy overhead.",
                }
            ],
            apply_to_children=False,
        )
        cloudtrail.Trail(
            self,
            "ManagementTrail",
            trail_name=f"{config.prefix}-management",
            bucket=trail_bucket,
            include_global_service_events=True,
            is_multi_region_trail=False,
            enable_file_validation=True,
            management_events=cloudtrail.ReadWriteType.ALL,
            send_to_cloud_watch_logs=False,
        )

