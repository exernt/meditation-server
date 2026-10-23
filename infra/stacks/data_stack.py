from __future__ import annotations

from aws_cdk import CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_secretsmanager as secretsmanager
from cdk_nag import NagSuppressions
from constructs import Construct

from infra.config import EnvironmentConfig


class DataStack(Stack):
    """Application credential for an externally provisioned Aurora Express cluster."""

    def __init__(self, scope: Construct, construct_id: str, *, config: EnvironmentConfig, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Free Plan Aurora clusters must be created with Express Configuration.
        # CloudFormation/CDK does not currently expose that create option, so the
        # cluster ARN is supplied as context and this stack owns only the separate
        # least-privilege application credential used by the Data API.
        self.application_secret = secretsmanager.Secret(
            self,
            "DatabaseApplicationSecret",
            secret_name=f"{config.prefix}/database/application",
            description=(
                "Credential for the wm_app PostgreSQL role used through the RDS Data API; "
                "not the Aurora Express IAM-only master identity"
            ),
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"username":"wm_app"}',
                generate_string_key="password",
                password_length=40,
                exclude_punctuation=True,
            ),
        )
        self.application_secret.apply_removal_policy(
            RemovalPolicy.RETAIN if config.is_production else RemovalPolicy.DESTROY
        )
        NagSuppressions.add_resource_suppressions(
            self.application_secret,
            [
                {
                    "id": "AwsSolutions-SMG4",
                    "reason": (
                        "Aurora Express requires an operator-created database role before automatic "
                        "rotation can be configured; the initialization and rotation runbooks coordinate "
                        "the PostgreSQL password and this secret."
                    ),
                }
            ],
            apply_to_children=False,
        )

        CfnOutput(
            self,
            "DatabaseApplicationSecretArn",
            value=self.application_secret.secret_arn,
            description="Secret to use when creating or rotating the wm_app database role",
        )
        CfnOutput(
            self,
            "AuroraExpressClusterArn",
            value=config.database_cluster_arn,
            description="Externally provisioned Free Plan Aurora Express cluster",
        )
