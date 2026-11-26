"""Crash-safe deletion recovery service for SiloVault.

Processes pending deletion records durable in the database, coordinating
physical cleanup across storage providers (LOCAL and GOOGLE_DRIVE) with
automatic retry scheduling, exponential backoff, atomic claiming, lease
expiration handling, and already-absent resource detection.
"""

from __future__ import annotations
import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, or_, and_
from sqlalchemy.orm import Session

from backend.models.pending_deletion import PendingDeletion
from backend.storage.file_account_resolution import resolve_file_adapter
from backend.storage.errors import (
    StorageNotFoundError,
    StorageAuthenticationError,
    StoragePermissionError,
    StorageUnavailableError,
    StorageRateLimitError,
    StorageUnsupportedError,
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
        from sqlalchemy import update

        now = datetime.now(timezone.utc)
        claim_token = str(uuid.uuid4())

        db: Session = self._db_factory()
        try:
            # 1. Find candidate IDs eligible for claiming (read-only scan).
            stmt = (
                select(PendingDeletion.id)
                .where(
                    and_(
                        or_(
                            PendingDeletion.status == "PENDING",
                            or_(
                                PendingDeletion.lease_expires_at.is_(None),
                                and_(
                                    PendingDeletion.lease_expires_at.isnot(None),
                                    PendingDeletion.lease_expires_at < now,
                                ),
                            ),
                        ),
                        or_(
                            PendingDeletion.next_retry_at.is_(None),
                            PendingDeletion.next_retry_at <= now,
                        ),
                        # Only CLEANUP and RESTORE records may be claimed.
                        PendingDeletion.operation_type.in_(
                            ["CLEANUP", "RESTORE"]
                        ),
                    )
                )
                .order_by(PendingDeletion.created_at.asc())
                .limit(batch_size)
            )
            candidate_ids = [row[0] for row in db.execute(stmt).all()]
            if not candidate_ids:
                return 0

            # 2. Atomically claim records using a conditional UPDATE.
            # The WHERE clause rechecks eligibility, so concurrent workers
            # cannot claim the same record. Only rows that match ALL conditions
            # are updated, and exactly one worker wins each race.
            update_stmt = (
                update(PendingDeletion)
                .where(
                    PendingDeletion.id.in_(candidate_ids),
                    PendingDeletion.status.in_(["PENDING", "PROCESSING"]),
                    # Recheck lease expiry for PROCESSING records
                    or_(
                        PendingDeletion.status == "PENDING",
                        or_(
                            PendingDeletion.lease_expires_at.is_(None),
                            and_(
                                PendingDeletion.lease_expires_at.isnot(None),
                                PendingDeletion.lease_expires_at < now,
                            ),
                        ),
                    ),
                    # Recheck retry schedule
                    or_(
                        PendingDeletion.next_retry_at.is_(None),
                        PendingDeletion.next_retry_at <= now,
                    ),
                    PendingDeletion.operation_type.in_(["CLEANUP", "RESTORE"]),
                )
                .values(
                    status="PROCESSING",
                    claim_token=claim_token,
                    operation_generation=PendingDeletion.operation_generation + 1,
                    processing_started_at=now,
                    lease_expires_at=now + timedelta(seconds=LEASE_DURATION_SECONDS),
                )
                .returning(
                    PendingDeletion.id,
                    PendingDeletion.operation_generation,
                )
            )

            # Execute atomic claim and capture claimed IDs with their generations.
            claimed_rows = db.execute(update_stmt).fetchall()
            db.commit()

            if not claimed_rows:
                return 0

            # Build a map of claimed ID -> generation captured at claim time.
            claimed_generations = {row[0]: int(row[1]) for row in claimed_rows}
            ids_to_process = list(claimed_generations.keys())

            # 3. Process each claimed record individually using the immutable
            # generation captured at claim time.
            success_count = 0
            for record_id in ids_to_process:
                record_stmt = select(PendingDeletion).where(
                    PendingDeletion.id == record_id
                )
                record = db.execute(record_stmt).scalar_one_or_none()
                if record is None or record.claim_token != claim_token:
                    # Lost the claim race after commit (should not happen)
                    continue
                claim_generation = claimed_generations[record_id]
                try:
                    if self._process_single_record(db, record, claim_token, claim_generation):
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

    def _process_single_record(self, db: Session, record: PendingDeletion, claim_token: str, claim_generation: int) -> bool:
        """Execute the operation for a single claimed deletion record.

        Dispatches to _execute_cleanup or _execute_restore based on
        record.operation_type.  Unknown or missing operation types are
        rejected before any adapter lookup.  The adapter is resolved by
        ``resolve_file_adapter``: legacy records (NULL provider_account_id)
        use the provider registry, managed records use exactly their own
        storage account, and a managed failure never falls back.

        Returns True if the record was successfully finalized (deleted from
        the queue); False if the record remains pending/failed and may be
        retried.
        """
        # 1. Validate operation_type before any provider lookup.
        #    For backward compatibility: if the column does not yet exist
        #    (Stage A schema), treat missing as unknown and skip.
        op_type = getattr(record, "operation_type", None)
        if op_type not in ("CLEANUP", "RESTORE"):
            logger.warning(
                "RecoveryService: unknown or missing operation_type %s for "
                "pending deletion %s – skipping",
                op_type,
                record.id,
            )
            return False

        # 2. Resolve the adapter for this record's exact owner.  Legacy records
        #    (NULL provider_account_id) use the provider registry; managed
        #    records use exactly their own storage account.  Any resolution
        #    failure - unknown account, provider mismatch, disconnected
        #    account, or unavailable store - schedules a retry and leaves the
        #    PendingDeletion row (and its ownership) intact.  A failed
        #    resolution is therefore never mistaken for a missing file.
        provider = record.provider
        try:
            adapter = resolve_file_adapter(db, record)
        except Exception as exc:
            logger.warning(
                "RecoveryService: could not resolve %s adapter for pending "
                "deletion %s (provider=%s, account=%s): %s",
                record.operation_type,
                record.id,
                provider,
                record.provider_account_id,
                exc,
            )
            self._schedule_retry(
                db, record, claim_token, claim_generation,
                f"Storage adapter unavailable: {exc}",
                transient=True,
            )
            return False

        # 3. Dispatch to the appropriate operation.
        if op_type == "CLEANUP":
            return self._execute_cleanup(db, record, claim_token, claim_generation, adapter)
        else:
            return self._execute_restore(db, record, claim_token, claim_generation, adapter)

    # ------------------------------------------------------------------
    # Operation helpers
    # ------------------------------------------------------------------

    def _execute_cleanup(self, db: Session, record: PendingDeletion, claim_token: str, claim_generation: int, adapter) -> bool:
        """Perform cleanup for a pending deletion.

        Returns True if cleanup succeeded and the pending deletion was
        finalized; False if a retry was scheduled or a permanent failure
        occurred.

        ``StorageNotFoundError`` is treated as successful cleanup only here,
        after ``_process_single_record`` resolved the record's exact adapter,
        so a missing resource in the right account stays idempotent.
        """
        quarantine_id = record.quarantine_id
        try:
            adapter.cleanup(quarantine_id)
            # Cleanup succeeded — permanently remove the pending deletion record
            success = self._finalize_success(db, record, claim_token, claim_generation)
            if success:
                logger.info(
                    "RecoveryService: successfully cleaned up pending deletion %s "
                    "(provider=%s, file=%s)",
                    record.id,
                    record.provider,
                    record.file_name_snapshot,
                )
            return success
        except StorageNotFoundError:
            # Resource is already absent — treat as successful cleanup
            success = self._finalize_success(db, record, claim_token, claim_generation)
            if success:
                logger.info(
                    "RecoveryService: resource already absent during cleanup for "
                    "pending deletion %s (provider=%s)",
                    record.id,
                    record.provider,
                )
            return success
        except (StorageAuthenticationError, StoragePermissionError) as exc:
            # Permanent failure — do not retry automatically; record failure
            self._handle_permanent_failure(db, record, claim_token, claim_generation, str(exc))
            logger.error(
                "RecoveryService: permanent authentication/permission failure for "
                "pending deletion %s: %s",
                record.id,
                exc,
            )
            return False
        except (StorageUnavailableError, StorageRateLimitError) as exc:
            # Temporary/transient failure — retry with backoff
            self._schedule_retry(
                db, record, claim_token, claim_generation, str(exc), transient=True,
                retry_after=getattr(exc, "retry_after_seconds", None),
            )
            logger.warning(
                "RecoveryService: transient failure for pending deletion %s "
                "(will retry): %s",
                record.id,
                exc,
            )
            return False
        except Exception as exc:
            # Other unexpected errors — treat as transient retryable
            self._schedule_retry(db, record, claim_token, claim_generation, str(exc), transient=True)
            logger.warning(
                "RecoveryService: unexpected error cleaning up pending deletion %s "
                "(will retry): %s",
                record.id,
                exc,
            )
            return False


    def _execute_restore(self, db: Session, record: PendingDeletion, claim_token: str, claim_generation: int, adapter) -> bool:
        """Restore a quarantined resource, handling an already-completed LOCAL restore.

        ``adapter`` is the record's exact account adapter, so the restore
        target (LOCAL root or cloud account) matches the original owner.
        """
        quarantine_id = record.quarantine_id

        try:
            try:
                adapter.restore(quarantine_id)

            except StorageNotFoundError as restore_exc:
                # A missing quarantine entry may mean a previous worker
                # restored the LOCAL file but crashed before finalization.
                if record.provider != "LOCAL":
                    raise

                expected_sha256 = record.original_sha256

                # Never assume success without a valid saved hash.
                if (
                    not isinstance(expected_sha256, str)
                    or len(expected_sha256) != 64
                    or any(c not in "0123456789abcdefABCDEF" for c in expected_sha256)
                ):
                    raise restore_exc

                # Verify the original physical file before treating this
                # operation as an already-completed restore.
                try:
                    original_bytes = adapter.download(record.provider_file_id)
                except Exception as verify_exc:
                    self._schedule_retry(
                        db, record, claim_token, claim_generation,
                        f"Restore verification failed: {verify_exc}",
                        transient=True,
                    )
                    logger.warning(
                        "RecoveryService: could not verify already-restored LOCAL file %s: %s",
                        record.id, verify_exc,
                    )
                    return False

                actual_sha256 = hashlib.sha256(original_bytes).hexdigest()

                if actual_sha256.lower() != expected_sha256.lower():
                    self._schedule_retry(
                        db, record, claim_token, claim_generation,
                        "LOCAL restore verification failed: SHA-256 mismatch.",
                        transient=True,
                    )
                    logger.warning(
                        "RecoveryService: SHA-256 mismatch for LOCAL restore %s",
                        record.id,
                    )
                    return False

                logger.info(
                    "RecoveryService: verified LOCAL file %s was already restored",
                    record.id,
                )

            # Normal restore or verified already-completed LOCAL restore.
            success = self._finalize_success(
                db, record, claim_token, claim_generation
            )

            if success:
                logger.info(
                    "RecoveryService: successfully restored pending deletion %s "
                    "(provider=%s, file=%s)",
                    record.id,
                    record.provider,
                    record.file_name_snapshot,
                )

            return success

        except StorageUnsupportedError as exc:
            self._handle_permanent_failure(
                db, record, claim_token, claim_generation, str(exc)
            )
            logger.error(
                "RecoveryService: restore unsupported for pending deletion %s: %s",
                record.id, exc,
            )
            return False

        except Exception as exc:
            self._schedule_retry(
                db, record, claim_token, claim_generation,
                str(exc), transient=True,
            )
            logger.warning(
                "RecoveryService: unexpected error restoring pending deletion %s "
                "(will retry): %s",
                record.id, exc,
            )
            return False

    # ------------------------------------------------------------------
    # Finalization and retry helpers
    # ------------------------------------------------------------------

    def _finalize_success(self, db: Session, record: PendingDeletion, claim_token: str, claim_generation: int) -> bool:
        """Atomically finalize a pending deletion record.

        Uses a conditional SQLAlchemy DELETE matching all five ownership
        conditions.  Only reports success when exactly one row was deleted.
        Zero affected rows means ownership was lost; do not claim success.

        Conditions (all required):
        1. PendingDeletion ID matches
        2. status = 'PROCESSING'
        3. matching claim_token
        4. lease_expires_at > current UTC time
        5. operation_generation == claim_generation (generation fencing)
        """
        from sqlalchemy import delete

        now = datetime.now(timezone.utc)

        stmt = delete(PendingDeletion).where(
            PendingDeletion.id == record.id,
            PendingDeletion.status == "PROCESSING",
            PendingDeletion.claim_token == claim_token,
            PendingDeletion.lease_expires_at > now,
            PendingDeletion.operation_generation == claim_generation,
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
                "(deleted_rows=%d; expected 1; token=%s, generation=%s)",
                record.id,
                deleted_rows,
                claim_token,
                claim_generation,
            )
            return False

    def _handle_permanent_failure(self, db: Session, record: PendingDeletion, claim_token: str, claim_generation: int, error_msg: str) -> None:
        """Mark record as FAILED permanently without further retries.

        Uses a conditional UPDATE with ownership + generation checks.
        Zero affected rows means ownership was lost (stale worker); the
        record is left untouched so the newer worker can manage it.

        Conditions (all required):
        1. PendingDeletion ID matches
        2. status = 'PROCESSING'
        3. matching claim_token
        4. lease_expires_at > current UTC time
        5. operation_generation == claim_generation (generation fencing)
        """
        from sqlalchemy import update

        now = datetime.now(timezone.utc)

        stmt = (
            update(PendingDeletion)
            .where(
                PendingDeletion.id == record.id,
                PendingDeletion.status == "PROCESSING",
                PendingDeletion.claim_token == claim_token,
                PendingDeletion.lease_expires_at > now,
                PendingDeletion.operation_generation == claim_generation,
            )
            .values(
                status="FAILED",
                last_error=error_msg,
                last_attempted_at=now,
                claim_token=None,
                processing_started_at=None,
                lease_expires_at=None,
            )
        )

        result = db.execute(stmt)
        updated_rows = result.rowcount

        if updated_rows == 1:
            db.commit()
        else:
            db.rollback()
            logger.warning(
                "B1.6b: _handle_permanent_failure: lost ownership for record %s "
                "(updated_rows=%d; expected 1; token=%s, generation=%s)",
                record.id,
                updated_rows,
                claim_token,
                claim_generation,
            )

    def _schedule_retry(
        self,
        db: Session,
        record: PendingDeletion,
        claim_token: str,
        claim_generation: int,
        error_msg: str,
        transient: bool = True,
        retry_after: Optional[int] = None,
    ) -> None:
        """Schedule a retry with exponential backoff or mark FAILED if max retries reached.

        Uses a conditional SQLAlchemy UPDATE with six ownership conditions to
        ensure atomicity.  Only modifies the record if all conditions are met,
        preventing TOCTOU races and lost-ownership scenarios.

        Six conditions (all required in the WHERE clause):
        1. PendingDeletion ID matches
        2. status = 'PROCESSING'
        3. matching claim_token
        4. lease_expires_at > current UTC time
        5. retry_count == expected (optimistic concurrency)
        6. operation_generation == claim_generation (generation fencing)

        If the UPDATE affects zero rows, ownership was lost; do not modify or
        retry.
        """
        from sqlalchemy import update

        now = datetime.now(timezone.utc)

        # Read the expected retry_count from the database using the current
        # session to ensure we have the fresh value for optimistic concurrency
        # check.
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
                PendingDeletion.operation_generation == claim_generation,
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
                "(updated_rows=%d; expected 1; token=%s, generation=%s)",
                record.id,
                updated_rows,
                claim_token,
                claim_generation,
            )
        else:
            db.commit()