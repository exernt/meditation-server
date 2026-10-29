from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from cdk_nag import AwsSolutionsChecks

from infra.config import EnvironmentConfig
from infra.stacks.api_stack import ApiStack
from infra.stacks.data_stack import DataStack
from infra.stacks.export_stack import ExportStack
from infra.stacks.generation_stack import GenerationStack
from infra.stacks.identity_stack import IdentityStack
from infra.stacks.operations_stack import OperationsStack


app = cdk.App()
region = str(app.node.try_get_context("region") or "ap-southeast-2").strip()
github_repository = str(app.node.try_get_context("githubRepository") or "").strip()
if not github_repository:
    raise ValueError("Missing required CDK context value: githubRepository")
if region != "ap-southeast-2":
    raise ValueError("Walking Meditation is intentionally restricted to ap-southeast-2")

environment = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=region,
)

identity = IdentityStack(
    app,
    "WmIdentity",
    github_repository=github_repository,
    env=environment,
    description="GitHub OIDC deployment identities",
)
stacks: list[cdk.Stack] = [identity]
stage = app.node.try_get_context("stage")
if stage is not None:
    config = EnvironmentConfig.from_app(app)
    data = DataStack(
        app,
        f"WmData-{config.stage}",
        config=config,
        env=environment,
        termination_protection=config.is_production,
    )
    generation = GenerationStack(
        app,
        f"WmGeneration-{config.stage}",
        config=config,
        data_stack=data,
        env=environment,
    )
    exports = ExportStack(
        app,
        f"WmExports-{config.stage}",
        config=config,
        data_stack=data,
        env=environment,
    )
    api = ApiStack(
        app,
        f"WmApi-{config.stage}",
        config=config,
        data_stack=data,
        generation_stack=generation,
        export_stack=exports,
        env=environment,
    )
    operations = OperationsStack(
        app,
        f"WmOperations-{config.stage}",
        config=config,
        export_stack=exports,
        env=environment,
    )
    stacks.extend((data, generation, exports, api, operations))

for stack in stacks:
    boundary = iam.ManagedPolicy.from_managed_policy_name(
        stack,
        "WmPermissionsBoundary",
        "WM-CdkBoundary",
    )
    iam.PermissionsBoundary.of(stack).apply(boundary)
    stack_tags = {
        "Project": "WalkingMeditation",
        "Environment": "shared" if stack is identity else str(stage),
        "DataClassification": "SensitiveResearch",
        "ManagedBy": "CDK",
    }
    for key, value in stack_tags.items():
        cdk.Tags.of(stack).add(key, value)
    cdk.Aspects.of(stack).add(AwsSolutionsChecks(verbose=True))

app.synth()

