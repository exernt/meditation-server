from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence


CONTEXT_VARIABLES = (
    ("stage", "STAGE", True),
    ("databaseClusterArn", "DATABASE_CLUSTER_ARN", True),
    ("apiReservedConcurrency", "API_RESERVED_CONCURRENCY", False),
    ("workerMaxConcurrency", "WORKER_MAX_CONCURRENCY", True),
    ("monthlyBudgetUsd", "MONTHLY_BUDGET_USD", True),
    ("budgetAlertEmail", "BUDGET_ALERT_EMAIL", True),
    ("operationalAlertEmail", "OPERATIONAL_ALERT_EMAIL", True),
    ("googleMapsSecretArn", "GOOGLE_MAPS_SECRET_ARN", True),
    ("openAiSecretArn", "OPENAI_SECRET_ARN", True),
    ("passwordResetFromEmail", "PASSWORD_RESET_FROM_EMAIL", True),
    ("passwordResetLinkBase", "PASSWORD_RESET_LINK_BASE", True),
    ("emailVerificationLinkBase", "EMAIL_VERIFICATION_LINK_BASE", True),
    ("responseExportLinkBase", "RESPONSE_EXPORT_LINK_BASE", True),
    ("ttsProvider", "TTS_PROVIDER", False),
    ("wafMode", "WAF_MODE", False),
)


def build_context_arguments(environment: Mapping[str, str]) -> list[str]:
    missing = [
        environment_name
        for _, environment_name, required in CONTEXT_VARIABLES
        if required and not environment.get(environment_name, "").strip()
    ]
    if missing:
        raise ValueError(
            "Missing required deployment environment variables: "
            + ", ".join(missing)
            + ". Configure them in the selected GitHub environment."
        )

    arguments: list[str] = []
    for context_name, environment_name, _ in CONTEXT_VARIABLES:
        value = environment.get(environment_name, "").strip()
        if value:
            arguments.extend(("-c", f"{context_name}={value}"))
    return arguments


def build_cdk_command(
    cdk_arguments: Sequence[str], environment: Mapping[str, str]
) -> list[str]:
    if not cdk_arguments:
        raise ValueError("A CDK command is required.")
    npx = shutil.which("npx") or "npx"
    action, *action_arguments = cdk_arguments
    return [
        npx,
        "cdk",
        action,
        *build_context_arguments(environment),
        *action_arguments,
    ]


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    try:
        if arguments == ["validate"]:
            build_context_arguments(os.environ)
            return 0
        command = build_cdk_command(arguments, os.environ)
    except ValueError as exc:
        print(f"CDK deployment configuration error: {exc}", file=sys.stderr)
        return 2

    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
