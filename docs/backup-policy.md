# Aurora backup and recovery policy

Aurora does not use a traditional weekly-full/daily-differential/hourly-log
schedule. Its automated backup is continuous and incremental and supports
point-in-time restore throughout the configured retention window.

## Policy

| Environment | Automated PITR | Manual snapshots | Restore testing |
| --- | --- | --- | --- |
| Development on Free Plan | 1 day | Before destructive migration only; delete after validation | Before production release |
| Synthetic production rehearsal on Free Plan | 1 day plus deletion protection | Before destructive migrations and final account handover; retain according to the approved research-data schedule | Before handover if the two-cluster account limit leaves room for the temporary restore; otherwise record an explicit client-owned exception |
| Production after paid-plan transition | 30 days plus deletion protection | Before destructive migrations and final account handover; retain 30 days unless the client requires longer | Monthly and before handover |

Aurora continuously captures the changes needed for point-in-time recovery, so
separate hourly transaction-log backups and daily differential backups are not
created. After the paid-plan transition, scheduled weekly snapshots inside the
same 30-day window add little recovery value. A release or pre-migration snapshot
is retained as an explicit operator checkpoint.

The restore objective is not satisfied until a restore has been tested. Each
test must record source recovery point, restored cluster identifier, start/end
time, schema checks, application smoke test, cleanup, and whether the measured
RPO/RTO met the client-approved targets.

The provisioning script applies these settings after Express creation, waits for
the cluster to return to `available`, and fails unless AWS reports the expected
backup retention, deletion-protection, Data API and PostgreSQL log-export state.
Deletion protection is a guard against accidental cluster deletion; it does not
replace PITR or a pre-handover manual snapshot.

## Cost treatment

As verified against AWS pricing on 23 October 2026:

- While a cluster is running, Aurora gives its automated backups an allowance
  equal to 100% of that cluster's current storage volume. Backup storage above
  that allowance is billable.
- After a cluster is deleted, retained automated backups are free for one day;
  storage retained beyond that day is billable. Copied or manual snapshots can
  also become billable and are not made permanently free by the Free Plan.
- The public on-demand Sydney price for Aurora PostgreSQL excess backup storage
  is **USD 0.023 per GB-month**. At that rate, 10 GiB, 50 GiB, and 100 GiB of
  billable excess are approximately USD 0.23, USD 1.15, and USD 2.30 per month.
- The current Aurora Free Plan requires Express Configuration and includes up
  to 4 ACUs and 1 GiB of database storage per eligible cluster, with at most two
  clusters/two instances per account, plus account credits. The one-off
  production rehearsal can use the second cluster only after checking the live
  account state and remaining credits. The plan does not make backup growth or
  retained snapshots perpetually free.

Monitor `BackupRetentionPeriodStorageUsed`, `SnapshotStorageUsed`, and
`TotalBackupStorageBilled`. Delete obsolete manual snapshots only after the
named data owner confirms the retention requirement has expired.

References: [Aurora backup storage](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-storage-backup.html),
[Aurora backup and restore](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Aurora.Managing.Backups.html),
and [Aurora Free Tier](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-free-tier.html).

