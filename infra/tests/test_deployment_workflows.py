from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_ci_synthesis_supplies_operational_alert_email() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "-c operationalAlertEmail=ci@example.invalid" in workflow


def test_deployment_passes_alert_context_and_deploys_alerting_stack() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "deploy.yml").read_text(
        encoding="utf-8"
    )

    context = "-c operationalAlertEmail=${{ vars.OPERATIONAL_ALERT_EMAIL }}"
    alerting_stack = "WmAlerting-${{ env.STAGE }}"
    assert workflow.count(context) == 3
    assert workflow.count(alerting_stack) == 2
