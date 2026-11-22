#!/usr/bin/env python3
"""Append the E2E test to test_ai_tasks_api.py"""

import os

FILEPATH = "tests/test_ai_tasks_api.py"

# Read the existing file
with open(FILEPATH, "r", encoding="utf-8") as f:
    content = f.read()

# The E2E test function
e2e_test = """
def test_end_to_end_worker_processes_and_approves(ai_env):
    \"\"\"Full E2E: API creates PENDING task \x96 worker processes to AWAITING_APPROVAL
    with a stored proposal \x96 API approval transitions to SUCCEEDED.

    This is the genuine end-to-end path: the worker-generated proposal is
    approved through the real FastAPI endpoint over the SAME isolated database.
    \"\"\"
    env = ai_env
    client = env["client"]
    engine = env["engine"]
    Session = env["Session"]

    # 1. Seed a file and capture its metadata before any AI work.
    record = _seed_file(engine)
    before = client.get(f"/api/v1/files/{record.id}").json()

    # 2. Create a task through the real API \x96 PENDING.
    created = _create_task(client, record.id)
    task_id = created["id"]
    assert created["status"] == "PENDING"

    # 3. Build a worker over the SAME database the API uses.
    from backend.services.ai_worker import AiWorkerService
    worker = AiWorkerService(lambda: Session())

    # 4. Register a harmless test processor that verifies it runs while the
    #    task is PROCESSING (proving the PROCESSING transition occurred).
    observed_states = []

    def harmless_processor(received_task_id):
        db = Session()
        try:
            task = db.get(AiTask, received_task_id)
            observed_states.append(task.status)
        finally:
            db.close()
        return {"category": "document", "confidence": 0.95}

    worker.register_processor("text_classification", harmless_processor)

    # 5. Execute the worker.
    processed = worker.process_one_task("worker-e2e", "text_classification")
    assert processed is True
    assert observed_states == ["PROCESSING"]

    # 6. Task is now AWAITING_APPROVAL with the proposal stored.
    detail = client.get(f"/api/v1/ai/tasks/{task_id}")
    assert detail.status_code == 200
    awaiting = detail.json()
    assert awaiting["status"] == "AWAITING_APPROVAL"
    assert awaiting["proposal_json"] == {"category": "document", "confidence": 0.95}
    assert awaiting["completed_at"] is not None
    assert awaiting["started_at"] is not None

    # 7. Approve through the real API endpoint.
    approve = client.post(f"/api/v1/ai/tasks/{task_id}/approve")
    assert approve.status_code == 200
    succeeded = approve.json()
    assert succeeded["status"] == "SUCCEEDED"
    assert succeeded["decision"] == "APPROVE"
    assert succeeded["decided_at"] is not None
    assert succeeded["completed_at"] is not None
    assert succeeded["proposal_json"] == {"category": "document", "confidence": 0.95}

    # 8. Internal worker fields are cleared in the DB.
    session = Session()
    try:
        fresh = session.get(AiTask, task_id)
        assert fresh is not None
        assert fresh.status == "SUCCEEDED"
        assert fresh.decision == "APPROVE"
        assert fresh.decided_at is not None
        assert fresh.completed_at is not None
        assert fresh.claim_token is None
        assert fresh.worker_id is None
        assert fresh.lease_expires_at is None
        assert fresh.proposal_json == {"category": "document", "confidence": 0.95}
    finally:
        session.close()

    # 9. Original file metadata is unchanged.
    after = client.get(f"/api/v1/files/{record.id}").json()
    assert before == after
"""

# Append the test after the last test (before trailing newlines)
# Find the position after the last test function
# The last test ends with: assert detail.json()["status"] == "REJECTED"
marker = 'assert detail.json()["status"] == "REJECTED"'
idx = content.rfind(marker)
if idx >= 0:
    # Insert the new test after this marker, preserving the original trailing content
    after_marker = content[idx + len(marker):]
    # Remove any leading/trailing whitespace/newlines from after_marker that are just blank lines
    content_new = content[:idx + len(marker)] + e2e_test + "\r\n" + after_marker
    with open(FILEPATH, "w", encoding="utf-8", newline="") as f:
        f.write(content_new)
    print("Test appended successfully")
else:
    print(f"Marker not found in file. Marker: {marker}")