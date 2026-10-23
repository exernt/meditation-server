[CmdletBinding()]
param(
    [ValidateSet("dev", "prod")]
    [string]$Stage = "dev",
    [string]$Profile = "wm-admin",
    [string]$Owner = "walking-meditation-team",
    [switch]$AllowProduction
)

$ErrorActionPreference = "Stop"
$region = "ap-southeast-2"
$clusterIdentifier = "wm-$Stage-aurora-postgres"

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

aws rds enable-http-endpoint `
    --resource-arn $cluster.DBClusterArn `
    --profile $Profile `
    --region $region | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Unable to enable the RDS Data API endpoint."
}

$retentionDays = if ($Stage -eq "prod") { 30 } else { 1 }
$protectionFlag = if ($Stage -eq "prod") { "--deletion-protection" } else { "--no-deletion-protection" }
aws rds modify-db-cluster `
    --db-cluster-identifier $clusterIdentifier `
    --backup-retention-period $retentionDays `
    --cloudwatch-logs-export-configuration EnableLogTypes=postgresql `
    $protectionFlag `
    --apply-immediately `
    --profile $Profile `
    --region $region | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Unable to apply the post-create Aurora settings."
}

$cluster = (aws rds describe-db-clusters `
    --db-cluster-identifier $clusterIdentifier `
    --profile $Profile `
    --region $region `
    --output json | ConvertFrom-Json).DBClusters[0]

[pscustomobject]@{
    AccountId = $identity.Account
    ClusterIdentifier = $cluster.DBClusterIdentifier
    ClusterArn = $cluster.DBClusterArn
    Endpoint = $cluster.Endpoint
    Port = $cluster.Port
    MasterUsername = $cluster.MasterUsername
    DatabaseName = "walking_meditation"
    DataApiEnabled = $cluster.HttpEndpointEnabled
    BackupRetentionDays = $retentionDays
    Status = $cluster.Status
} | Format-List
