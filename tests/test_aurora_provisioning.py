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


def test_production_backup_and_deletion_protection_are_applied_and_verified() -> None:
    assert '$retentionDays = if ($Stage -eq "prod") { 30 } else { 1 }' in SCRIPT
    assert '$protectionFlag = if ($Stage -eq "prod") { "--deletion-protection" }' in SCRIPT
    assert '$expectedDeletionProtection = $Stage -eq "prod"' in SCRIPT
    assert "BackupRetentionPeriod -ne $retentionDays" in SCRIPT
    assert "DeletionProtection -ne $expectedDeletionProtection" in SCRIPT
    assert "BackupRetentionDays = $cluster.BackupRetentionPeriod" in SCRIPT
    assert "DeletionProtection = $cluster.DeletionProtection" in SCRIPT
