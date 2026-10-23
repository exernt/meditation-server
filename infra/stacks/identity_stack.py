from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack, aws_iam as iam
from constructs import Construct


class IdentityStack(Stack):
    """GitHub OIDC provider and tightly scoped CDK deployment roles."""

    def __init__(self, scope: Construct, construct_id: str, *, github_repository: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        provider = iam.OpenIdConnectProvider(
            self,
            "GitHubOidcProvider",
            url="https://token.actions.githubusercontent.com",
            client_ids=["sts.amazonaws.com"],
        )

        for stage, environment_name in (("dev", "dev"), ("prod", "production")):
            principal = iam.FederatedPrincipal(
                provider.open_id_connect_provider_arn,
                conditions={
                    "StringEquals": {
                        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                        "token.actions.githubusercontent.com:sub": (
                            f"repo:{github_repository}:environment:{environment_name}"
                        ),
                    }
                },
                assume_role_action="sts:AssumeRoleWithWebIdentity",
            )
            role = iam.Role(
                self,
                f"GitHubDeployer{stage.title()}",
                role_name=f"wm-{stage}-github-deployer",
                assumed_by=principal,
                max_session_duration=Duration.hours(1),
                description=f"GitHub Actions deployment role for {stage}",
            )
            qualifier = "hnb659fds"
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="AssumeCdkBootstrapRoles",
                    actions=["sts:AssumeRole"],
                    resources=[
                        f"arn:{self.partition}:iam::{self.account}:role/cdk-{qualifier}-{role_type}-role-{self.account}-{self.region}"
                        for role_type in ("deploy", "file-publishing", "image-publishing", "lookup")
                    ],
                )
            )
            CfnOutput(
                self,
                f"GitHubDeployer{stage.title()}RoleArn",
                value=role.role_arn,
                description=f"GitHub Actions deployment role for the {environment_name} environment",
            )

