"""Apply Repair 2: Atomic Recovery Claiming"""
import re

path = r'c:\Projects\SiloVault\backend\services\recovery.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# Replace the entire process_pending_deletions method
old_method = '''    def process_pending_deletions(self, batch_size: int = 10) -> int:
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
                        # Only CLEANUP and RESTORE records may be claimed.
                        # NULL or unknown operation types are skipped entirely.
                        PendingDeletion.operation_type.in_(
                            ["CLEANUP", "RESTORE"]
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
            # Also capture the newly assigned operation_generation for each claim.
            claimed_generations: dict[str, int] = {}
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
                record.operation_generation = (getattr(record, "operation_generation", 0) or 0) + 1
                record.processing_started_at = now
                record.lease_expires_at = now + timedelta(seconds=LEASE_DURATION_SECONDS)
                claimed_generations[record.id] = int(record.operation_generation)

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

            # 3. Process each claimed record individually using the immutable
            # generation captured at claim time.
            success_count = 0
            for record_id in ids_to_process:
                record_stmt = select(PendingDeletion).where(
                    PendingDeletion.id == record_id
                )
                record = db.execute(record_stmt).scalar_one_or_none()
                if record is None or record.claim_token != claim_token:
                    # Lost the claim race — skip
                    continue
                claim_generation = claimed_generations.get(record_id, int(getattr(record, "operation_generation", 0) or 0))
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
            db.close()'''

new_method = '''    def process_pending_deletions(self, batch_size: int = 10) -> int:
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
                            and_(
                                PendingDeletion.status == "PROCESSING",
                                PendingDeletion.lease_expires_at < now,
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
                        and_(
                            PendingDeletion.lease_expires_at.isnot(None),
                            PendingDeletion.lease_expires_at < now,
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
            db.close()'''

if old_method not in content:
    print("ERROR: old method not found in file")
    exit(1)

content = content.replace(old_method, new_method, 1)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Edit successful")
