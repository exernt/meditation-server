from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import urlsplit

from aws_cdk import App


def _required(app: App, key: str) -> Any:
    value = app.node.try_get_context(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"Missing required CDK context value: {key}")
    return value


def _number(app: App, key: str) -> float:
    try:
        return float(_required(app, key))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"CDK context {key} must be numeric") from exc


def _optional_positive_integer(app: App, key: str) -> int | None:
    value = app.node.try_get_context(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"CDK context {key} must be a positive integer when set") from exc
    if not parsed.is_integer() or parsed < 1:
        raise ValueError(f"CDK context {key} must be a positive integer when set")
    return int(parsed)


@dataclass(frozen=True)
class EnvironmentConfig:
    project: str
    stage: str
    region: str
    github_repository: str
    database_cluster_arn: str
    database_name: str
    api_reserved_concurrency: int | None
    worker_max_concurrency: int
    monthly_budget_usd: float
    budget_alert_email: str
    operational_alert_email: str
    google_maps_secret_arn: str
    openai_secret_arn: str
    password_reset_from_email: str
    password_reset_link_base: str
    email_verification_link_base: str
    response_export_link_base: str
    tts_provider: str
    waf_mode: str

    @property
    def is_production(self) -> bool:
        return self.stage == "prod"

    @property
    def prefix(self) -> str:
        return f"{self.project}-{self.stage}"

    @classmethod
    def from_app(cls, app: App) -> "EnvironmentConfig":
        stage = str(_required(app, "stage")).strip().casefold()
        if stage not in {"dev", "prod"}:
            raise ValueError("CDK context stage must be dev or prod")

        region = str(app.node.try_get_context("region") or "ap-southeast-2").strip()
        database_cluster_arn = str(_required(app, "databaseClusterArn")).strip()
        cluster_arn_pattern = re.compile(
            rf"^arn:[^:]+:rds:{re.escape(region)}:\d{{12}}:cluster:[A-Za-z0-9-]+$"
        )
        if not cluster_arn_pattern.fullmatch(database_cluster_arn):
            raise ValueError(
                "databaseClusterArn must be an Aurora cluster ARN in the configured Region"
            )

        database_name = str(app.node.try_get_context("databaseName") or "walking_meditation").strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,62}", database_name):
            raise ValueError(
                "databaseName must start with a letter and contain only letters, numbers, or underscores"
            )

        tts_provider = str(app.node.try_get_context("ttsProvider") or "polly").strip().casefold()
        if tts_provider not in {"disabled", "polly"}:
            raise ValueError("ttsProvider must be disabled or polly")

        waf_mode = str(app.node.try_get_context("wafMode") or "count").strip().casefold()
        if waf_mode not in {"count", "block"}:
            raise ValueError("wafMode must be count or block")

        password_reset_link = str(_required(app, "passwordResetLinkBase")).strip()
        parsed_password_reset_link = urlsplit(password_reset_link)
        if (
            parsed_password_reset_link.scheme != "https"
            or not parsed_password_reset_link.netloc
            or not parsed_password_reset_link.path.endswith("/auth/password-reset/open")
            or parsed_password_reset_link.query
            or parsed_password_reset_link.fragment
        ):
            raise ValueError(
                "passwordResetLinkBase must be an HTTPS URL ending in "
                "/auth/password-reset/open with no query or fragment"
            )

        verification_link = str(_required(app, "emailVerificationLinkBase")).strip()
        parsed_link = urlsplit(verification_link)
        if (
            parsed_link.scheme != "https"
            or not parsed_link.netloc
            or not parsed_link.path.endswith("/auth/email-verification/open")
            or parsed_link.query
            or parsed_link.fragment
        ):
            raise ValueError(
                "emailVerificationLinkBase must be an HTTPS URL ending in "
                "/auth/email-verification/open with no query or fragment"
            )

        export_link = str(_required(app, "responseExportLinkBase")).strip().rstrip("/")
        parsed_export_link = urlsplit(export_link)
        if (
            parsed_export_link.scheme != "https"
            or not parsed_export_link.netloc
            or not parsed_export_link.path.endswith("/response-exports")
            or parsed_export_link.query
            or parsed_export_link.fragment
        ):
            raise ValueError(
                "responseExportLinkBase must be an HTTPS URL ending in "
                "/response-exports with no query or fragment"
            )

        return cls(
            project=str(app.node.try_get_context("project") or "wm").strip(),
            stage=stage,
            region=region,
            github_repository=str(_required(app, "githubRepository")).strip(),
            database_cluster_arn=database_cluster_arn,
            database_name=database_name,
            api_reserved_concurrency=_optional_positive_integer(
                app, "apiReservedConcurrency"
            ),
            worker_max_concurrency=int(_number(app, "workerMaxConcurrency")),
            monthly_budget_usd=_number(app, "monthlyBudgetUsd"),
            budget_alert_email=str(_required(app, "budgetAlertEmail")).strip(),
            operational_alert_email=str(
                _required(app, "operationalAlertEmail")
            ).strip(),
            google_maps_secret_arn=str(_required(app, "googleMapsSecretArn")).strip(),
            openai_secret_arn=str(_required(app, "openAiSecretArn")).strip(),
            password_reset_from_email=str(_required(app, "passwordResetFromEmail")).strip(),
            password_reset_link_base=password_reset_link,
            email_verification_link_base=verification_link,
            response_export_link_base=export_link,
            tts_provider=tts_provider,
            waf_mode=waf_mode,
        )

