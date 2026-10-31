[CmdletBinding()]
param(
    [ValidateSet("dev", "prod")]
    [string]$Stage = "dev",
    [string]$Profile = "wm-admin",
    [string]$Owner = "walking-meditation-team",
    [switch]$AllowProduction,
    [switch]$ApplyLoggingParameterGroup,
    [ValidateRange(1, 35)]
    [int]$BackupRetentionDays = 1
)

$ErrorActionPreference = "Stop"
$region = "ap-southeast-2"
$clusterIdentifier = "wm-$Stage-aurora-postgres"
$script:pendingRebootWarningEmitted = $false

function Invoke-AwsCliCaptured {
    param(
        [Parameter(Mandatory)]
        [string[]]$AwsArguments
    )

    # Windows PowerShell promotes native stderr to NativeCommandError records.
    # With the script-wide Stop preference, that can terminate the script before
    # it can inspect aws.exe's exit code and report the actual AWS response.
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = @(& aws @AwsArguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = @($output | ForEach-Object { $_.ToString() })
    }
}

function Get-AuroraClusterState {
    $clusterJson = aws rds describe-db-clusters `
        --db-cluster-identifier $clusterIdentifier `
        --profile $Profile `
        --region $region `
        --output json
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to read Aurora cluster state while waiting for a modification."
    }

    $currentCluster = ($clusterJson | ConvertFrom-Json).DBClusters[0]
    if (-not $currentCluster) {
        throw "Aurora cluster '$clusterIdentifier' was not returned while waiting for a modification."
    }
    return $currentCluster
}

function Wait-AuroraClusterState {
    param(
        [string]$ExpectedParameterGroup = "",
        [switch]$RequireDataApi,
        [int]$ExpectedBackupRetentionDays = -1,
        [AllowNull()]
        [Nullable[bool]]$ExpectedDeletionProtection = $null,
        [switch]$RequirePostgresqlLogExport,
        [int]$MaxAttempts = 60,
        [int]$DelaySeconds = 10
    )

    $lastCluster = $null
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        $lastCluster = Get-AuroraClusterState
        $members = @($lastCluster.DBClusterMembers)
        $pendingRebootMembers = @(
            $members | Where-Object {
                $_.DBClusterParameterGroupStatus -eq "pending-reboot"
            }
        )
        $parameterGroupReady = [string]::IsNullOrWhiteSpace($ExpectedParameterGroup) -or (
            $lastCluster.DBClusterParameterGroup -eq $ExpectedParameterGroup -and
            $members.Count -gt 0 -and
            @(
                $members | Where-Object {
                    $_.DBClusterParameterGroupStatus -notin @(
                        "in-sync",
                        "pending-reboot"
                    )
                }
            ).Count -eq 0
        )
        $dataApiReady = -not $RequireDataApi -or [bool]$lastCluster.HttpEndpointEnabled
        $backupReady = (
            $ExpectedBackupRetentionDays -lt 0 -or
            [int]$lastCluster.BackupRetentionPeriod -eq $ExpectedBackupRetentionDays
        )
        $deletionProtectionReady = (
            $null -eq $ExpectedDeletionProtection -or
            [bool]$lastCluster.DeletionProtection -eq [bool]$ExpectedDeletionProtection
        )
        $logExportReady = (
            -not $RequirePostgresqlLogExport -or
            $lastCluster.EnabledCloudwatchLogsExports -contains "postgresql"
        )

        if (
            $lastCluster.Status -eq "available" -and
            $parameterGroupReady -and
            $dataApiReady -and
            $backupReady -and
            $deletionProtectionReady -and
            $logExportReady
        ) {
            if (
                $pendingRebootMembers.Count -gt 0 -and
                -not $script:pendingRebootWarningEmitted
            ) {
                Write-Warning (
                    "The Aurora cluster parameter group is attached but pending a " +
                    "maintenance reboot on: " +
                    ($pendingRebootMembers.DBInstanceIdentifier -join ", ") +
                    ". Continuing with settings that do not require that reboot."
                )
                $script:pendingRebootWarningEmitted = $true
            }
            return $lastCluster
        }

        if ($attempt -lt $MaxAttempts) {
            Write-Host (
                "Waiting for Aurora changes to settle " +
                "(attempt $attempt/$MaxAttempts, status=$($lastCluster.Status))..."
            )
            Start-Sleep -Seconds $DelaySeconds
        }
    }

    $memberStatuses = @(
        $lastCluster.DBClusterMembers |
            ForEach-Object { $_.DBClusterParameterGroupStatus }
    ) -join ","
    throw (
        "Timed out waiting for Aurora changes to settle. " +
        "Status=$($lastCluster.Status); " +
        "ParameterGroup=$($lastCluster.DBClusterParameterGroup); " +
        "MemberParameterGroupStatuses=$memberStatuses; " +
        "DataApiEnabled=$($lastCluster.HttpEndpointEnabled); " +
        "BackupRetentionDays=$($lastCluster.BackupRetentionPeriod); " +
        "DeletionProtection=$($lastCluster.DeletionProtection); " +
        "LogExports=$($lastCluster.EnabledCloudwatchLogsExports -join ',')."
    )
}

if ($Stage -eq "prod" -and -not $AllowProduction) {
    throw "Production provisioning requires -AllowProduction after the account and release gates are approved."
}

$versionOutput = aws --version 2>&1
if ($LASTEXITCODE -ne 0 -or $versionOutput -notmatch "aws-cli/(\d+)\.(\d+)") {
    throw "AWS CLI v2.33.0 or later is required."
}
$major = [int]$Matches[1]
$minor = [int]$Matches[2]
if ($major -lt 2 -or ($major -eq 2 -and $minor -lt 33)) {
    throw "Aurora Express provisioning requires AWS CLI v2.33.0 or later; found $versionOutput"
}

$identity = aws sts get-caller-identity --profile $Profile --region $region | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) {
    throw "Unable to read the current AWS identity. Run aws login for profile '$Profile'."
}
if ($identity.Arn -match ":root$") {
    throw "Refusing to provision with the AWS account root user."
}

$clustersJson = aws rds describe-db-clusters `
    --profile $Profile `
    --region $region `
    --output json
if ($LASTEXITCODE -ne 0) {
    throw "Unable to list Aurora clusters in account '$($identity.Account)' and region '$region'."
}
$clusters = $clustersJson | ConvertFrom-Json
$clusterExists = @(
    $clusters.DBClusters | Where-Object { $_.DBClusterIdentifier -eq $clusterIdentifier }
).Count -gt 0
$clusterWasCreated = -not $clusterExists

if (-not $clusterExists) {
    Write-Host "Creating Aurora PostgreSQL cluster '$clusterIdentifier' with Express Configuration..."
    aws rds create-db-cluster `
        --db-cluster-identifier $clusterIdentifier `
        --engine aurora-postgresql `
        --with-express-configuration `
        --profile $Profile `
        --region $region `
        --tags `
            Key=Project,Value=WalkingMeditation `
            Key=Environment,Value=$Stage `
            Key=DataClassification,Value=SensitiveResearch `
            Key=ManagedBy,Value=ExternalPrerequisite `
            Key=Owner,Value=$Owner `
            Key=created_by,Value=aurora-skill `
            Key=generation_model,Value=gpt-5 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Aurora Express creation failed."
    }

    aws rds wait db-cluster-available `
        --db-cluster-identifier $clusterIdentifier `
        --profile $Profile `
        --region $region
    if ($LASTEXITCODE -ne 0) {
        throw "Timed out waiting for Aurora Express to become available."
    }
} else {
    Write-Host "Cluster '$clusterIdentifier' already exists; leaving it in place."
}

$cluster = (aws rds describe-db-clusters `
    --db-cluster-identifier $clusterIdentifier `
    --profile $Profile `
    --region $region `
    --output json | ConvertFrom-Json).DBClusters[0]
if ($LASTEXITCODE -ne 0) {
    throw "Unable to read Aurora cluster details."
}

$engineMajor = [int](($cluster.EngineVersion -split "\.")[0])
$parameterGroupFamily = "aurora-postgresql$engineMajor"
$loggingParameterGroup = "wm-$Stage-aurora-postgres-$engineMajor-logging"
$slowQueryMilliseconds = if ($Stage -eq "prod") { 1000 } else { 500 }

$parameterGroupsJson = aws rds describe-db-cluster-parameter-groups `
    --profile $Profile `
    --region $region `
    --output json
if ($LASTEXITCODE -ne 0) {
    throw "Unable to inspect Aurora PostgreSQL cluster parameter groups."
}
$parameterGroups = $parameterGroupsJson | ConvertFrom-Json
$matchingParameterGroup = @(
    $parameterGroups.DBClusterParameterGroups |
        Where-Object { $_.DBClusterParameterGroupName -eq $loggingParameterGroup }
) | Select-Object -First 1
if (-not $matchingParameterGroup) {
    Write-Host "Creating Aurora PostgreSQL logging parameter group '$loggingParameterGroup'..."
    aws rds create-db-cluster-parameter-group `
        --db-cluster-parameter-group-name $loggingParameterGroup `
        --db-parameter-group-family $parameterGroupFamily `
        --description "Walking Meditation $Stage privacy-preserving PostgreSQL logging" `
        --profile $Profile `
        --region $region `
        --tags `
            Key=Project,Value=WalkingMeditation `
            Key=Environment,Value=$Stage `
            Key=DataClassification,Value=SensitiveResearch `
            Key=ManagedBy,Value=ExternalPrerequisite | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to create Aurora PostgreSQL logging parameter group."
    }
} elseif ($matchingParameterGroup.DBParameterGroupFamily -ne $parameterGroupFamily) {
    throw (
        "Logging parameter group '$loggingParameterGroup' belongs to " +
        "'$($matchingParameterGroup.DBParameterGroupFamily)', expected '$parameterGroupFamily'."
    )
}

$loggingParameters = @(
    "ParameterName=log_statement,ParameterValue=none,ApplyMethod=immediate"
    "ParameterName=log_min_duration_statement,ParameterValue=$slowQueryMilliseconds,ApplyMethod=immediate"
    "ParameterName=log_parameter_max_length,ParameterValue=0,ApplyMethod=immediate"
    "ParameterName=log_parameter_max_length_on_error,ParameterValue=0,ApplyMethod=immediate"
    "ParameterName=log_min_messages,ParameterValue=warning,ApplyMethod=immediate"
    "ParameterName=log_min_error_statement,ParameterValue=error,ApplyMethod=immediate"
    "ParameterName=log_lock_waits,ParameterValue=1,ApplyMethod=immediate"
    "ParameterName=deadlock_timeout,ParameterValue=1000,ApplyMethod=immediate"
    "ParameterName=log_temp_files,ParameterValue=10240,ApplyMethod=immediate"
    "ParameterName=log_autovacuum_min_duration,ParameterValue=5000,ApplyMethod=immediate"
    "ParameterName=rds.force_autovacuum_logging_level,ParameterValue=warning,ApplyMethod=immediate"
    "ParameterName=log_connections,ParameterValue=0,ApplyMethod=immediate"
    "ParameterName=log_disconnections,ParameterValue=0,ApplyMethod=immediate"
    "ParameterName=log_duration,ParameterValue=0,ApplyMethod=immediate"
)
aws rds modify-db-cluster-parameter-group `
    --db-cluster-parameter-group-name $loggingParameterGroup `
    --parameters $loggingParameters `
    --profile $Profile `
    --region $region | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Unable to configure Aurora PostgreSQL logging parameters."
}

$requiredParameterGroup = if (
    $cluster.DBClusterParameterGroup -eq $loggingParameterGroup
) { $loggingParameterGroup } else { "" }
if ($cluster.DBClusterParameterGroup -ne $loggingParameterGroup) {
    if ($clusterWasCreated) {
        aws rds modify-db-cluster `
            --db-cluster-identifier $clusterIdentifier `
            --db-cluster-parameter-group-name $loggingParameterGroup `
            --apply-immediately `
            --profile $Profile `
            --region $region | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to attach the PostgreSQL logging parameter group to the new cluster."
        }
        $requiredParameterGroup = $loggingParameterGroup
    } elseif ($ApplyLoggingParameterGroup) {
        aws rds modify-db-cluster `
            --db-cluster-identifier $clusterIdentifier `
            --db-cluster-parameter-group-name $loggingParameterGroup `
            --no-apply-immediately `
            --profile $Profile `
            --region $region | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to schedule the PostgreSQL logging parameter group association."
        }
    } else {
        Write-Warning (
            "Logging parameters are configured in '$loggingParameterGroup', but the existing " +
            "cluster still uses '$($cluster.DBClusterParameterGroup)'. Review the pending-reboot " +
            "impact, then rerun with -ApplyLoggingParameterGroup to schedule the association."
        )
    }
}

$cluster = Wait-AuroraClusterState `
    -ExpectedParameterGroup $requiredParameterGroup

aws rds enable-http-endpoint `
    --resource-arn $cluster.DBClusterArn `
    --profile $Profile `
    --region $region | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Unable to enable the RDS Data API endpoint."
}

$cluster = Wait-AuroraClusterState `
    -ExpectedParameterGroup $requiredParameterGroup `
    -RequireDataApi

$retentionDays = $BackupRetentionDays
$cloudWatchLogRetentionDays = if ($Stage -eq "prod") { 90 } else { 14 }
$postgresLogGroup = "/aws/rds/cluster/$clusterIdentifier/postgresql"
$logGroupsJson = aws logs describe-log-groups `
    --log-group-name-prefix $postgresLogGroup `
    --profile $Profile `
    --region $region `
    --output json
if ($LASTEXITCODE -ne 0) {
    throw "Unable to inspect the PostgreSQL CloudWatch log group."
}
$logGroups = $logGroupsJson | ConvertFrom-Json
$logGroupExists = @(
    $logGroups.logGroups | Where-Object { $_.logGroupName -eq $postgresLogGroup }
).Count -gt 0
if (-not $logGroupExists) {
    aws logs create-log-group `
        --log-group-name $postgresLogGroup `
        --profile $Profile `
        --region $region `
        --tags "Project=WalkingMeditation,Environment=$Stage,DataClassification=SensitiveResearch,ManagedBy=ExternalPrerequisite" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to create the PostgreSQL CloudWatch log group."
    }
}
aws logs put-retention-policy `
    --log-group-name $postgresLogGroup `
    --retention-in-days $cloudWatchLogRetentionDays `
    --profile $Profile `
    --region $region | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Unable to configure PostgreSQL CloudWatch log retention."
}

$expectedDeletionProtection = $Stage -eq "prod"
$protectionFlag = if ($expectedDeletionProtection) {
    "--deletion-protection"
} else {
    "--no-deletion-protection"
}
$postCreateModifyResult = Invoke-AwsCliCaptured -AwsArguments @(
    "rds"
    "modify-db-cluster"
    "--db-cluster-identifier"
    $clusterIdentifier
    "--backup-retention-period"
    $retentionDays.ToString()
    "--cloudwatch-logs-export-configuration"
    "EnableLogTypes=postgresql"
    $protectionFlag
    "--apply-immediately"
    "--profile"
    $Profile
    "--region"
    $region
)
if ($postCreateModifyResult.ExitCode -ne 0) {
    throw (
        "Unable to apply the post-create Aurora settings. AWS CLI output: " +
        ($postCreateModifyResult.Output -join [Environment]::NewLine)
    )
}

$cluster = Wait-AuroraClusterState `
    -ExpectedParameterGroup $requiredParameterGroup `
    -RequireDataApi `
    -ExpectedBackupRetentionDays $retentionDays `
    -ExpectedDeletionProtection $expectedDeletionProtection `
    -RequirePostgresqlLogExport
if ([int]$cluster.BackupRetentionPeriod -ne $retentionDays) {
    throw (
        "Aurora backup retention verification failed: expected $retentionDays days, " +
        "found $($cluster.BackupRetentionPeriod)."
    )
}
if ([bool]$cluster.DeletionProtection -ne $expectedDeletionProtection) {
    throw (
        "Aurora deletion-protection verification failed: expected " +
        "$expectedDeletionProtection, found $($cluster.DeletionProtection)."
    )
}
if (-not $cluster.HttpEndpointEnabled) {
    throw "Aurora Data API verification failed: the HTTP endpoint is not enabled."
}
if ($cluster.EnabledCloudwatchLogsExports -notcontains "postgresql") {
    throw "Aurora log-export verification failed: PostgreSQL export is not enabled."
}

[pscustomobject]@{
    AccountId = $identity.Account
    ClusterIdentifier = $cluster.DBClusterIdentifier
    ClusterArn = $cluster.DBClusterArn
    Endpoint = $cluster.Endpoint
    Port = $cluster.Port
    MasterUsername = $cluster.MasterUsername
    DatabaseName = "walking_meditation"
    DataApiEnabled = $cluster.HttpEndpointEnabled
    BackupRetentionDays = $cluster.BackupRetentionPeriod
    DeletionProtection = $cluster.DeletionProtection
    CloudWatchLogRetentionDays = $cloudWatchLogRetentionDays
    LoggingParameterGroup = $loggingParameterGroup
    ActiveParameterGroup = $cluster.DBClusterParameterGroup
    ParameterGroupStatus = @(
        $cluster.DBClusterMembers | ForEach-Object { $_.DBClusterParameterGroupStatus }
    ) -join ","
    Status = $cluster.Status
} | Format-List
