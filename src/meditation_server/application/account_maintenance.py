from __future__ import annotations

from meditation_server.repositories.executor import DatabaseExecutor


UNVERIFIED_ACCOUNT_RETENTION_DAYS = 7
UNVERIFIED_ACCOUNT_CLEANUP_BATCH_SIZE = 500
UNVERIFIED_ACCOUNT_CLEANUP_MAX_BATCHES = 10


async def cleanup_unverified_accounts(database: DatabaseExecutor) -> dict[str, int]:
    """Remove accounts that were never verified after the grace period."""
    sql = f"""WITH candidates AS (
                   SELECT id FROM users
                    WHERE email_verified = false
                      AND created_on < now() - interval '{UNVERIFIED_ACCOUNT_RETENTION_DAYS} days'
                    ORDER BY created_on, id
                    LIMIT {UNVERIFIED_ACCOUNT_CLEANUP_BATCH_SIZE}
               ),
               deleted AS (
                   DELETE FROM users AS expired_user
                    USING candidates
                    WHERE expired_user.id = candidates.id
                      AND expired_user.email_verified = false
                    RETURNING expired_user.id
               )
               INSERT INTO security_events (event_type)
               SELECT 'unverified_account_expired' FROM deleted"""

    deleted = 0
    for _ in range(UNVERIFIED_ACCOUNT_CLEANUP_MAX_BATCHES):
        count = (await database.execute(sql)).records_updated
        deleted += count
        if count < UNVERIFIED_ACCOUNT_CLEANUP_BATCH_SIZE:
            break
    return {"unverified_accounts_deleted": deleted}
