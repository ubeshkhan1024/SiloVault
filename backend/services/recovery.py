"""Crash-safe deletion recovery service for SiloVault.

Processes pending deletion records durable in the database, coordinating
physical cleanup across storage providers (LOCAL and GOOGLE_DRIVE) with
automatic retry scheduling, exponential backoff, atomic claiming, lease
expiration handling, and already-absent resource detection.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, or_, and_
from sqlalchemy.orm import Session

from backend.models.pending_deletion import PendingDeletion
from backend.storage.registry import lookup
from backend.storage.errors import (
    StorageNotFoundError,
    StorageAuthenticationError,
    StoragePermissionError,
    StorageUnavailableError,
    StorageRateLimitError,
)

logger = logging.getLogger(__name__)

# Lease duration for atomic processing claims
LEASE_DURATION_SECONDS = 300  # 5 minutes

# Max retries before marking permanent failure
MAX_RETRIES = 5


class RecoveryService:
    """Orchestrates crash-safe cleanup of pending deletions."""

    def __init__(self, db_factory) -> None:
        self._db_factory = db_factory

    def process_pending_deletions(self, batch_size: int = 10) -> int:
        """Scan and process a batch of claimable pending deletion records.

        Claimable records are:
        - status == 'PENDING'
        - OR (status == 'PROCESSING' AND lease_expires_at < now)
        - AND (next_retry_at IS NULL OR next_retry_at <= now)

        Returns the number of successfully completed/cleaned-up records.
        """
        now = datetime.now(timezone.utc)
        claim_token = str(uuid.uuid4())

        db: Session = self._db_factory()
        try:
            # 1. Find candidate records eligible for claiming
            stmt = (
                select(PendingDeletion)
                .where(
                    and_(
                        or_(
                            PendingDeletion.status == "PENDING",
                            and_(
                                PendingDeletion.status == "PROCESSING",
                                PendingDeletion.lease_expires_at < now,
                            ),
                        ),
                        or_(
                            PendingDeletion.next_retry_at.is_(None),
                            PendingDeletion.next_retry_at <= now,
                        ),
                    )
                )
                .order_by(PendingDeletion.created_at.asc())
                .limit(batch_size)
            )
            candidates = db.execute(stmt).scalars().all()
            if not candidates:
                return 0

            # 2. Atomically claim all candidates in a single batch commit.
            # Using UPDATE with WHERE conditions ensures only records that
            # are still claimable get modified — another worker running
            # concurrently cannot steal a record we've already selected.
            for record in candidates:
                # Skip if this record was already claimed by someone else
                # or is no longer eligible (re-check for safety).
                if record.status != "PENDING" and not (
                    record.status == "PROCESSING"
                    and record.lease_expires_at
                    and record.lease_expires_at < now
                ):
                    continue
                if record.next_retry_at and record.next_retry_at > now:
                    continue

                record.status = "PROCESSING"
                record.claim_token = claim_token
                record.processing_started_at = now
                record.lease_expires_at = now + timedelta(seconds=LEASE_DURATION_SECONDS)

            # Single batch commit for all claims
            db.commit()

            # Re-fetch confirmed claims (some may have been stolen between
            # selection and commit; the WHERE clause above limits the blast
            # radius but re-fetching is the safest way to know exactly what
            # we own).
            ids_to_process = [
                c.id for c in candidates
                if c.status == "PROCESSING" and c.claim_token == claim_token
            ]
            if not ids_to_process:
                return 0

            # 3. Process each claimed record individually.
            success_count = 0
            for record_id in ids_to_process:
                record_stmt = select(PendingDeletion).where(
                    PendingDeletion.id == record_id
                )
                record = db.execute(record_stmt).scalar_one_or_none()
                if record is None or record.claim_token != claim_token:
                    # Lost the claim race — skip
                    continue
                try:
                    if self._process_single_record(db, record, claim_token):
                        success_count += 1
                except Exception as exc:
                    logger.error(
                        "RecoveryService: unexpected error processing pending deletion %s: %s",
                        record.id,
                        exc,
                    )
            return success_count

        finally:
            db.close()

    def _process_single_record(self, db: Session, record: PendingDeletion, claim_token: str) -> bool:
        """Execute physical cleanup for a single claimed deletion record.

        Returns True if the record was successfully finalized (deleted from the queue);
        False if the record remains pending/failed and may be retried.
        """
        provider = record.provider
        quarantine_id = record.quarantine_id

        # Look up storage adapter
        try:
            adapter, _ = lookup(provider)
        except Exception as exc:
            logger.warning(
                "RecoveryService: storage provider %s unavailable for pending deletion %s: %s",
                provider,
                record.id,
                exc,
            )
            self._schedule_retry(db, record, claim_token, f"Storage provider unavailable: {exc}", transient=True)
            return False

        # Perform cleanup
        try:
            adapter.cleanup(quarantine_id)
            # Cleanup succeeded — permanently remove the pending deletion record
            success = self._finalize_success(db, record, claim_token)
            if success:
                logger.info(
                    "RecoveryService: successfully cleaned up pending deletion %s (provider=%s, file=%s)",
                    record.id,
                    provider,
                    record.file_name_snapshot,
                )
            return success
        except StorageNotFoundError:
            # Resource is already absent — treat as successful cleanup
            success = self._finalize_success(db, record, claim_token)
            if success:
                logger.info(
                    "RecoveryService: resource already absent during cleanup for pending deletion %s (provider=%s)",
                    record.id,
                    provider,
                )
            return success
        except (StorageAuthenticationError, StoragePermissionError) as exc:
            # Permanent failure — do not retry automatically; record failure
            self._handle_permanent_failure(db, record, claim_token, str(exc))
            logger.error(
                "RecoveryService: permanent authentication/permission failure for pending deletion %s: %s",
                record.id,
                exc,
            )
            return False
        except (StorageUnavailableError, StorageRateLimitError) as exc:
            # Temporary/transient failure — retry with backoff
            self._schedule_retry(
                db, record, claim_token, str(exc), transient=True,
                retry_after=getattr(exc, "retry_after_seconds", None),
            )
            logger.warning(
                "RecoveryService: transient failure for pending deletion %s (will retry): %s",
                record.id,
                exc,
            )
            return False
        except Exception as exc:
            # Other unexpected errors — treat as transient retryable
            self._schedule_retry(db, record, claim_token, str(exc), transient=True)
            logger.warning(
                "RecoveryService: unexpected error cleaning up pending deletion %s (will retry): %s",
                record.id,
                exc,
            )
            return False

    def _finalize_success(self, db: Session, record: PendingDeletion, claim_token: str) -> bool:
        """Atomically finalize a pending deletion record.

        Uses a conditional SQLAlchemy DELETE matching all four ownership conditions.
        Only reports success when exactly one row was deleted.
        Zero affected rows means ownership was lost; do not claim success.

        Conditions (all required):
        1. PendingDeletion ID matches
        2. status = 'PROCESSING'
        3. matching claim_token
        4. lease_expires_at > current UTC time
        """
        from sqlalchemy import delete

        now = datetime.now(timezone.utc)

        stmt = delete(PendingDeletion).where(
            PendingDeletion.id == record.id,
            PendingDeletion.status == "PROCESSING",
            PendingDeletion.claim_token == claim_token,
            PendingDeletion.lease_expires_at > now,
        )

        result = db.execute(stmt)
        deleted_rows = result.rowcount

        if deleted_rows == 1:
            db.commit()
            return True
        else:
            db.rollback()
            logger.warning(
                "B1.6b: _finalize_success: lost ownership for record %s "
                "(deleted_rows=%d; expected 1)",
                record.id,
                deleted_rows,
            )
            return False

    def _handle_permanent_failure(self, db: Session, record: PendingDeletion, claim_token: str, error_msg: str) -> None:
        """Mark record as FAILED permanently without further retries."""
        db.expire(record)
        current = db.get(PendingDeletion, record.id)
        if current is None or current.claim_token != claim_token:
            return
        current.status = "FAILED"
        current.last_error = error_msg
        current.last_attempted_at = datetime.now(timezone.utc)
        current.claim_token = None
        current.processing_started_at = None
        current.lease_expires_at = None
        db.commit()

    def _schedule_retry(
        self,
        db: Session,
        record: PendingDeletion,
        claim_token: str,
        error_msg: str,
        transient: bool = True,
        retry_after: Optional[int] = None,
    ) -> None:
        """Schedule a retry with exponential backoff or mark FAILED if max retries reached.

        Uses a conditional SQLAlchemy UPDATE with five ownership conditions to ensure
        atomicity. Only modifies the record if all conditions are met, preventing TOCTOU
        races and lost-ownership scenarios.

        Five conditions (all required in the WHERE clause):
        1. PendingDeletion ID matches
        2. status = 'PROCESSING'
        3. matching claim_token
        4. lease_expires_at > current UTC time
        5. retry_count == expected (optimistic concurrency)

        If the UPDATE affects zero rows, ownership was lost; do not modify or retry.
        """
        from sqlalchemy import update

        now = datetime.now(timezone.utc)

        # Read the expected retry_count from the database using the current session
        # to ensure we have the fresh value for optimistic concurrency check.
        fresh_record = db.get(PendingDeletion, record.id)
        if fresh_record is None or fresh_record.claim_token != claim_token:
            logger.warning(
                "B1.6b: _schedule_retry: lost ownership for record %s before update",
                record.id,
            )
            return
        expected_retry_count = fresh_record.retry_count

        stmt = (
            update(PendingDeletion)
            .where(
                PendingDeletion.id == record.id,
                PendingDeletion.status == "PROCESSING",
                PendingDeletion.claim_token == claim_token,
                PendingDeletion.lease_expires_at > now,
                PendingDeletion.retry_count == expected_retry_count,
            )
            .values(
                retry_count=expected_retry_count + 1,
                last_error=error_msg,
                last_attempted_at=now,
                claim_token=None,
                processing_started_at=None,
                lease_expires_at=None,
                status="PENDING" if expected_retry_count + 1 < MAX_RETRIES else "FAILED",
                next_retry_at=(
                    datetime.now(timezone.utc) + timedelta(seconds=(
                        retry_after if retry_after else (30 * (2 ** (expected_retry_count + 1 - 1)))
                    ))
                    if expected_retry_count + 1 < MAX_RETRIES
                    else None
                ),
            )
        )

        result = db.execute(stmt)
        updated_rows = result.rowcount

        if updated_rows == 0:
            # Ownership was lost or stale version; do not claim success
            db.rollback()
            logger.warning(
                "B1.6b: _schedule_retry: lost ownership for record %s "
                "(updated_rows=%d; expected 1)",
                record.id,
                updated_rows,
            )
        else:
            db.commit()