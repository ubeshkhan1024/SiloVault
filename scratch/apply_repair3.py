"""Apply Repair 3: Generation Fencing in recovery.py"""

path = r'c:\Projects\SiloVault\backend\services\recovery.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

replacements = []

# --- 1. _process_single_record: pass claim_generation to _schedule_retry and execute helpers ---
old = '''            self._schedule_retry(
                db, record, claim_token,
                f"Storage provider unavailable: {exc}",
                transient=True,
            )
            return False

        # 3. Dispatch to the appropriate operation.
        if op_type == "CLEANUP":
            return self._execute_cleanup(db, record, claim_token, adapter)
        else:
            return self._execute_restore(db, record, claim_token, adapter)'''
new = '''            self._schedule_retry(
                db, record, claim_token, claim_generation,
                f"Storage provider unavailable: {exc}",
                transient=True,
            )
            return False

        # 3. Dispatch to the appropriate operation.
        if op_type == "CLEANUP":
            return self._execute_cleanup(db, record, claim_token, claim_generation, adapter)
        else:
            return self._execute_restore(db, record, claim_token, claim_generation, adapter)'''
replacements.append((old, new))

# --- 2. _execute_cleanup signature + all internal calls ---
old = '''    def _execute_cleanup(self, db: Session, record: PendingDeletion, claim_token: str, adapter) -> bool:'''
new = '''    def _execute_cleanup(self, db: Session, record: PendingDeletion, claim_token: str, claim_generation: int, adapter) -> bool:'''
replacements.append((old, new))

old = '''        quarantine_id = record.quarantine_id
        try:
            adapter.cleanup(quarantine_id)
            # Cleanup succeeded — permanently remove the pending deletion record
            success = self._finalize_success(db, record, claim_token)
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
            success = self._finalize_success(db, record, claim_token)
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
            self._handle_permanent_failure(db, record, claim_token, str(exc))
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
                db, record, claim_token, str(exc), transient=True,
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
            self._schedule_retry(db, record, claim_token, str(exc), transient=True)
            logger.warning(
                "RecoveryService: unexpected error cleaning up pending deletion %s "
                "(will retry): %s",
                record.id,
                exc,
            )
            return False'''
new = '''        quarantine_id = record.quarantine_id
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
            return False'''
replacements.append((old, new))

# --- 3. _execute_restore signature + all internal calls ---
old = '''    def _execute_restore(self, db: Session, record: PendingDeletion, claim_token: str, adapter) -> bool:'''
new = '''    def _execute_restore(self, db: Session, record: PendingDeletion, claim_token: str, claim_generation: int, adapter) -> bool:'''
replacements.append((old, new))

old = '''        quarantine_id = record.quarantine_id
        try:
            adapter.restore(quarantine_id)
            # Restore succeeded — permanently remove the pending deletion record
            success = self._finalize_success(db, record, claim_token)
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
            # Unsupported restore operation — permanent failure, do not
            # fall back to cleanup.
            self._handle_permanent_failure(db, record, claim_token, str(exc))
            logger.error(
                "RecoveryService: restore unsupported for pending deletion %s: %s",
                record.id,
                exc,
            )
            return False
        except Exception as exc:
            # Any other error is treated as transient and retried
            self._schedule_retry(db, record, claim_token, str(exc), transient=True)
            logger.warning(
                "RecoveryService: unexpected error restoring pending deletion %s "
                "(will retry): %s",
                record.id,
                exc,
            )
            return False'''
new = '''        quarantine_id = record.quarantine_id
        try:
            adapter.restore(quarantine_id)
            # Restore succeeded — permanently remove the pending deletion record
            success = self._finalize_success(db, record, claim_token, claim_generation)
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
            # Unsupported restore operation — permanent failure, do not
            # fall back to cleanup.
            self._handle_permanent_failure(db, record, claim_token, claim_generation, str(exc))
            logger.error(
                "RecoveryService: restore unsupported for pending deletion %s: %s",
                record.id,
                exc,
            )
            return False
        except Exception as exc:
            # Any other error is treated as transient and retried
            self._schedule_retry(db, record, claim_token, claim_generation, str(exc), transient=True)
            logger.warning(
                "RecoveryService: unexpected error restoring pending deletion %s "
                "(will retry): %s",
                record.id,
                exc,
            )
            return False'''
replacements.append((old, new))

# --- 4. _finalize_success: add generation check ---
old = '''    def _finalize_success(self, db: Session, record: PendingDeletion, claim_token: str) -> bool:
        """Atomically finalize a pending deletion record.

        Uses a conditional SQLAlchemy DELETE matching all four ownership
        conditions.  Only reports success when exactly one row was deleted.
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
            return False'''
new = '''    def _finalize_success(self, db: Session, record: PendingDeletion, claim_token: str, claim_generation: int) -> bool:
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
            return False'''
replacements.append((old, new))

# --- 5. _handle_permanent_failure: conditional UPDATE with generation check ---
old = '''    def _handle_permanent_failure(self, db: Session, record: PendingDeletion, claim_token: str, error_msg: str) -> None:
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
        db.commit()'''
new = '''    def _handle_permanent_failure(self, db: Session, record: PendingDeletion, claim_token: str, claim_generation: int, error_msg: str) -> None:
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
            )'''
replacements.append((old, new))

# --- 6. _schedule_retry: signature + generation check ---
old = '''    def _schedule_retry(
        self,
        db: Session,
        record: PendingDeletion,
        claim_token: str,
        error_msg: str,
        transient: bool = True,
        retry_after: Optional[int] = None,
    ) -> None:
        """Schedule a retry with exponential backoff or mark FAILED if max retries reached.

        Uses a conditional SQLAlchemy UPDATE with five ownership conditions to
        ensure atomicity.  Only modifies the record if all conditions are met,
        preventing TOCTOU races and lost-ownership scenarios.

        Five conditions (all required in the WHERE clause):
        1. PendingDeletion ID matches
        2. status = 'PROCESSING'
        3. matching claim_token
        4. lease_expires_at > current UTC time
        5. retry_count == expected (optimistic concurrency)

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
            db.commit()'''
new = '''    def _schedule_retry(
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
            db.commit()'''
replacements.append((old, new))

for i, (old, new) in enumerate(replacements, 1):
    if old not in content:
        print(f"ERROR: replacement {i} target not found")
        exit(1)
    content = content.replace(old, new, 1)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print("All 6 replacements applied successfully")
