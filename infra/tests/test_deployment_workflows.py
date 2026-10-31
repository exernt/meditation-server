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


def test_deployment_runs_smoke_tests_with_environment_secrets() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "deploy.yml").read_text(
        encoding="utf-8"
    )

    assert "--outputs-file cdk-outputs.json" in workflow
    assert "json.load(open('cdk-outputs.json'" in workflow
    assert "aws cloudformation describe-stacks" not in workflow
    assert "python scripts/post_deployment_smoke.py" in workflow
    assert "SMOKE_TEST_EMAIL: ${{ secrets.SMOKE_TEST_EMAIL }}" in workflow
    assert "SMOKE_TEST_PASSWORD: ${{ secrets.SMOKE_TEST_PASSWORD }}" in workflow
    assert "SMOKE_TEST_TTS_VOICE: ${{ vars.SMOKE_TEST_TTS_VOICE }}" in workflow
