from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "provision-aurora-express.ps1"
).read_text(encoding="utf-8")


def test_postgresql_cloudwatch_retention_is_stage_specific() -> None:
    assert '$cloudWatchLogRetentionDays = if ($Stage -eq "prod") { 90 } else { 14 }' in SCRIPT
    assert "aws logs put-retention-policy" in SCRIPT
    assert '"/aws/rds/cluster/$clusterIdentifier/postgresql"' in SCRIPT


def test_postgresql_logging_parameters_exclude_sensitive_values() -> None:
    expected = {
        "log_statement": "none",
        "log_parameter_max_length": "0",
        "log_parameter_max_length_on_error": "0",
        "log_min_messages": "warning",
        "log_min_error_statement": "error",
        "log_lock_waits": "1",
        "log_temp_files": "10240",
        "log_autovacuum_min_duration": "5000",
        "rds.force_autovacuum_logging_level": "warning",
        "log_connections": "0",
        "log_disconnections": "0",
        "log_duration": "0",
    }
    for name, value in expected.items():
        assert f"ParameterName={name},ParameterValue={value},ApplyMethod=immediate" in SCRIPT

    assert (
        'ParameterName=log_min_duration_statement,ParameterValue=$slowQueryMilliseconds,'
        "ApplyMethod=immediate"
    ) in SCRIPT


def test_existing_cluster_parameter_group_change_requires_explicit_switch() -> None:
    assert "[switch]$ApplyLoggingParameterGroup" in SCRIPT
    assert "elseif ($ApplyLoggingParameterGroup)" in SCRIPT
    assert "--no-apply-immediately" in SCRIPT


def test_backup_retention_defaults_to_free_plan_maximum_and_is_verified() -> None:
    assert "[ValidateRange(1, 35)]" in SCRIPT
    assert "[int]$BackupRetentionDays = 1" in SCRIPT
    assert "$retentionDays = $BackupRetentionDays" in SCRIPT
    assert '$expectedDeletionProtection = $Stage -eq "prod"' in SCRIPT
    assert "$protectionFlag = if ($expectedDeletionProtection) {" in SCRIPT
    assert '"--deletion-protection"' in SCRIPT
    assert '"--no-deletion-protection"' in SCRIPT
    assert "BackupRetentionPeriod -ne $retentionDays" in SCRIPT
    assert "DeletionProtection -ne $expectedDeletionProtection" in SCRIPT
    assert "BackupRetentionDays = $cluster.BackupRetentionPeriod" in SCRIPT
    assert "DeletionProtection = $cluster.DeletionProtection" in SCRIPT


def test_serializes_post_create_cluster_modifications() -> None:
    assert "function Wait-AuroraClusterState" in SCRIPT
    assert 'DBClusterParameterGroupStatus -notin @(' in SCRIPT
    assert '"in-sync",' in SCRIPT
    assert '"pending-reboot"' in SCRIPT
    assert "-RequireDataApi" in SCRIPT
    assert "-RequirePostgresqlLogExport" in SCRIPT
    assert "-ExpectedBackupRetentionDays $retentionDays" in SCRIPT
    assert "-ExpectedDeletionProtection $expectedDeletionProtection" in SCRIPT

    parameter_group_wait = SCRIPT.index(
        "$cluster = Wait-AuroraClusterState `\n"
        "    -ExpectedParameterGroup $requiredParameterGroup"
    )
    data_api_call = SCRIPT.index("aws rds enable-http-endpoint")
    data_api_wait = SCRIPT.index(
        "$cluster = Wait-AuroraClusterState `\n"
        "    -ExpectedParameterGroup $requiredParameterGroup `\n"
        "    -RequireDataApi"
    )
    settings_call = SCRIPT.index(
        "$postCreateModifyResult = Invoke-AwsCliCaptured -AwsArguments @("
    )
    assert parameter_group_wait < data_api_call < data_api_wait < settings_call


def test_post_create_failure_includes_aws_cli_output() -> None:
    assert "$postCreateModifyResult = Invoke-AwsCliCaptured -AwsArguments @(" in SCRIPT
    assert "if ($postCreateModifyResult.ExitCode -ne 0)" in SCRIPT
    assert "$postCreateModifyResult.Output -join" in SCRIPT
    assert "AWS CLI output:" in SCRIPT


def test_aws_stderr_cannot_bypass_exit_code_handling() -> None:
    assert "function Invoke-AwsCliCaptured" in SCRIPT
    assert '$ErrorActionPreference = "Continue"' in SCRIPT
    assert "$exitCode = $LASTEXITCODE" in SCRIPT
    assert "$ErrorActionPreference = $previousErrorActionPreference" in SCRIPT


def test_pending_reboot_is_reported_without_blocking_provisioning() -> None:
    assert "$script:pendingRebootWarningEmitted = $false" in SCRIPT
    assert "maintenance reboot" in SCRIPT
    assert "Continuing with settings that do not require that reboot." in SCRIPT
