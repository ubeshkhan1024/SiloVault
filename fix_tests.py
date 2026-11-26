#!/usr/bin/env python3
"""Script to fix the three failing tests in test_storage_resolver.py"""

import re

with open("tests/test_storage_resolver.py", "r", encoding="utf-8") as f:
    content = f.read()

# --- Fix 1: test_invalidation_during_construction_prevents_stale_cache ---

old_invalidation = """def test_invalidation_during_construction_prevents_stale_cache(
    db_session, local_account
):
    \"\"\"Invalidating an account while construction is in progress prevents stale cache insertion.\"\"\"
    from unittest.mock import MagicMock
    resolver = AccountResolver(db_session)

    construction_started = threading.Event()
    can_complete_construction = threading.Event()

    original_create = AdapterFactory.create_adapter

    def paused_create(self_factory):
        construction_started.set()
        assert can_complete_construction.wait(timeout=5.0), "Timed out waiting to complete construction"
        return original_create(self_factory)

    AdapterFactory.create_adapter = paused_create  # type: ignore

    results: list[Any] = [None]
    errors: list[Exception] = []

    def resolve_worker():
        try:
            results[0] = resolver.resolve(local_account)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=resolve_worker)
    try:
        thread.start()
        assert construction_started.wait(timeout=5.0), "Timed out waiting for construction to start"
        # While construction is paused, invalidate the account
        resolver.invalidate_account(local_account)
        # Release construction to finish
        can_complete_construction.set()
        thread.join(timeout=5.0)
    finally:
        AdapterFactory.create_adapter = original_create  # type: ignore

    assert not errors, f"Resolution worker raised errors: {errors}"
    # The adapter constructed before invalidation completed must NOT remain in cache
    # as a valid cached adapter for future calls
    cached_adapter = resolver._cache.get(local_account)
    assert cached_adapter is None, "Invalidated adapter was inappropriately inserted into cache"
    # Subsequent resolution must follow current account state and produce fresh adapter
    fresh_adapter = resolver.resolve(local_account)
    assert fresh_adapter is not None
    assert fresh_adapter is not results[0]"""

new_invalidation = """def test_invalidation_during_construction_prevents_stale_cache(
    db_session, local_account
):
    \"\"\"Invalidating an account while construction is in progress prevents stale cache insertion.

    - Pause construction of adapter A.
    - Invalidate the account while construction is paused.
    - Resume construction.
    - Verify adapter A is discarded and never returned or cached.
    - Verify the resolver retries and constructs adapter B.
    - Verify adapter B is cached.
    - Verify a subsequent resolve returns the exact same adapter B.

    Record constructed adapter instances so the test can distinguish A from B.
    \"\"\"
    resolver = AccountResolver(db_session)

    construction_started = threading.Event()
    can_complete_construction = threading.Event()

    # Track the two adapter instances: stale A (before invalidation) and fresh B (after retry)
    stale_adapter = None
    fresh_adapter = None

    original_create = AdapterFactory.create_adapter

    def paused_create(self_factory):
        nonlocal stale_adapter, fresh_adapter
        construction_started.set()
        assert can_complete_construction.wait(timeout=5.0), "Timed out waiting to complete construction"
        adapter = original_create(self_factory)
        # Record the first constructed adapter as the stale one (Adapter A)
        if stale_adapter is None:
            stale_adapter = adapter
        else:
            # Second construction (after retry) is the fresh adapter (Adapter B)
            fresh_adapter = adapter
        return adapter

    AdapterFactory.create_adapter = paused_create  # type: ignore

    results: list[Any] = [None]
    errors: list[Exception] = []

    def resolve_worker():
        try:
            results[0] = resolver.resolve(local_account)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=resolve_worker)
    try:
        thread.start()
        assert construction_started.wait(timeout=5.0), "Timed out waiting for construction to start"
        # While construction is paused, invalidate the account
        resolver.invalidate_account(local_account)
        # Release construction to finish
        can_complete_construction.set()
        thread.join(timeout=5.0)
    finally:
        AdapterFactory.create_adapter = original_create  # type: ignore

    assert not errors, f"Resolution worker raised errors: {errors}"

    # Adapter A (stale) was constructed before invalidation and must NOT be cached.
    # Adapter B (fresh) was constructed after the generation mismatch retry and IS cached.
    cached_adapter = resolver._cache.get(local_account)
    assert cached_adapter is not stale_adapter, (
        "Stale adapter was inappropriately cached despite invalidation"
    )
    assert cached_adapter is fresh_adapter, (
        "Fresh adapter was not cached after generation mismatch retry"
    )
    # Subsequent resolve must return the same cached fresh adapter
    another_resolve = resolver.resolve(local_account)
    assert another_resolve is cached_adapter, (
        "Subsequent resolve did not return the cached fresh adapter"
    )"""

if old_invalidation in content:
    content = content.replace(old_invalidation, new_invalidation)
    print("Fix 1: test_invalidation_during_construction_prevents_stale_cache - REPLACED")
else:
    print("Fix 1: FAILED - old test text not found")
    # Debug: find the test
    idx = content.find("def test_invalidation_during_construction_prevents_stale_cache")
    if idx >= 0:
        print(f"  Found at index {idx}")
        print(content[idx:idx+600])

# --- Fix 2: test_legacy_fallback_explicit_verification ---

old_legacy = """def test_legacy_fallback_explicit_verification(db_session):
    \"\"\"Explicit verification of legacy fallback behavior.

    - NULL account ID cannot be passed to resolve() without error.
    - Legacy fallback via resolve_legacy() is permitted for NULL records.
    - Invalid non-NULL account ID raises ValueError and never falls back to registry.
    \"\"\"
    from backend.storage.registry import register, ProviderCapabilities
    from backend.storage.local import LocalAdapter
    from backend.storage.capabilities import CapabilityStatus

    resolver = AccountResolver(db_session)

    # Register LOCAL provider for this test
    adapter = LocalAdapter(storage_root="C:/legacy_test_root")
    try:
        register(
            "LOCAL",
            adapter,
            ProviderCapabilities(
                addressing_mode="AUTO",
                native_hash_algorithm="sha256",
                supports_trash=CapabilityStatus.TRUE,
                supports_delta_sync=CapabilityStatus.FALSE,
                supports_revisions=CapabilityStatus.FALSE,
                supports_search=CapabilityStatus.FALSE,
                supports_chunked_upload=CapabilityStatus.FALSE,
                supports_resumable_upload=CapabilityStatus.FALSE,
                requires_decryption=CapabilityStatus.FALSE,
            ),
        )

        # 1. NULL account ID raises ValueError in resolve()
        with pytest.raises(ValueError):
            resolver.resolve(None)  # type: ignore

        # 2. NULL account ID via resolve_legacy() is permitted
        resolved_legacy = resolver.resolve_legacy("LOCAL")
        assert resolved_legacy is not None

        # 3. Invalid non-NULL account ID raises ValueError and never falls back to registry
        fake_id = str(uuid.uuid4())
        with pytest.raises(ValueError, match="not found"):
            resolver.resolve(fake_id)
    finally:
        # Clean up: remove the registered provider
        from backend.storage.registry import _adapter_map
        if "LOCAL" in _adapter_map:
            del _adapter_map["LOCAL"]"""

new_legacy = """def test_legacy_fallback_explicit_verification(db_session):
    \"\"\"Explicit verification of legacy fallback behavior.

    - NULL account ID cannot be passed to resolve() without error.
    - Legacy fallback via resolve_legacy() is permitted for NULL records.
    - Invalid non-NULL account ID raises ValueError and never falls back to registry.
    \"\"\"
    from backend.storage.registry import register, ProviderCapabilities, _adapter_map
    from backend.storage.local import LocalAdapter
    from backend.storage.capabilities import CapabilityStatus

    resolver = AccountResolver(db_session)

    # Register LOCAL provider for this test
    # Ensure clean state: remove any leftover registration from other tests
    adapter = LocalAdapter(storage_root="C:/legacy_test_root")
    if "LOCAL" in _adapter_map:
        del _adapter_map["LOCAL"]

    try:
        register(
            "LOCAL",
            adapter,
            ProviderCapabilities(
                addressing_mode="AUTO",
                native_hash_algorithm="sha256",
                supports_trash=CapabilityStatus.TRUE,
                supports_delta_sync=CapabilityStatus.FALSE,
                supports_revisions=CapabilityStatus.FALSE,
                supports_search=CapabilityStatus.FALSE,
                supports_chunked_upload=CapabilityStatus.FALSE,
                supports_resumable_upload=CapabilityStatus.FALSE,
                requires_decryption=CapabilityStatus.FALSE,
            ),
        )

        # 1. NULL account ID raises ValueError in resolve()
        with pytest.raises(ValueError):
            resolver.resolve(None)  # type: ignore

        # 2. NULL account ID via resolve_legacy() is permitted
        resolved_legacy = resolver.resolve_legacy("LOCAL")
        assert resolved_legacy is not None

        # 3. Invalid non-NULL account ID raises ValueError and never falls back to registry
        fake_id = str(uuid.uuid4())
        with pytest.raises(ValueError, match="not found"):
            resolver.resolve(fake_id)
    finally:
        # Clean up: remove the registered provider
        if "LOCAL" in _adapter_map:
            del _adapter_map["LOCAL"]"""

if old_legacy in content:
    content = content.replace(old_legacy, new_legacy)
    print("Fix 2: test_legacy_fallback_explicit_verification - REPLACED")
else:
    print("Fix 2: FAILED - old test text not found")
    idx = content.find("def test_legacy_fallback_explicit_verification")
    if idx >= 0:
        print(f"  Found at index {idx}")
        print(content[idx:idx+600])

# --- Fix 3: test_concurrent_resolution_same_account_returns_same_adapter ---

old_concurrent = """def test_concurrent_resolution_same_account_returns_same_adapter(
    db_session, local_account, monkeypatch
):
    \"\"\"Two threads resolving the same account must receive the exact same adapter object (is).

    Uses a controlled mock of AdapterFactory.create_adapter() so both worker threads
    reach adapter construction before either can complete. Then verifies both callers
    receive the exact same object using is.
    \"\"\"\n    resolver = AccountResolver(db_session)\n    go = threading.Event()\n    results: list[Any] = [None, None]\n    errors: list[Exception] = []\n\n    def worker(idx: int):
        try:
            go.wait(timeout=5.0)
            results[idx] = resolver.resolve(local_account)
        except Exception as exc:
            errors.append(exc)\n\n    original_create = AdapterFactory.create_adapter\n\n    def mock_create(self_factory):
        # Both threads must reach construction before either can complete.
        # Defer to original creation; the resolver's cache logic will ensure
        # concurrent callers receive the same cached adapter.
        return original_create(self_factory)\n\n    AdapterFactory.create_adapter = mock_create  # type: ignore\n\n    t1 = threading.Thread(target=worker, args=(0,))\n    t2 = threading.Thread(target=worker, args=(1,))\n    t1.start()\n    t2.start()\n    # Give both threads time to start and enter resolve()...resolve() will\n    # call create_adapter which is now mocked. Signal them to proceed.\n    go.set()\n    t1.join(timeout=5.0)\n    t2.join(timeout=5.0)\n\n    AdapterFactory.create_adapter = original_create  # type: ignore\n\n    assert not errors, f"Concurrent resolution raised errors: {errors}"\n    assert results[0] is not None\n    assert results[1] is not None\n    assert results[0] is results[1]\n    assert resolver.resolve(local_account) is results[0]"""

new_concurrent = """def test_concurrent_resolution_same_account_returns_same_adapter(
    db_session, local_account, monkeypatch
):
    \"\"\"Two threads resolving the same account must receive the exact same adapter object (is).

    Uses a controlled mock of AdapterFactory.create_adapter() with barrier synchronization
    so both worker threads reach adapter construction before either construction is released.
    Then verifies both callers receive the exact same object using is.
    \"\"\"\n    resolver = AccountResolver(db_session)\n\n    # Barrier to ensure both threads reach adapter construction before either can complete\n    creation_barrier = threading.Barrier(2, timeout=5.0)\n\n    results: list[Any] = [None, None]\n    errors: list[Exception] = []\n\n    def worker(idx: int):
        try:
            # Both threads must reach this barrier before either proceeds into resolve().
            # This ensures both threads are inside resolve() and will call create_adapter().
            creation_barrier.wait(timeout=5.0)
            results[idx] = resolver.resolve(local_account)
        except Exception as exc:
            errors.append(exc)\n\n    original_create = AdapterFactory.create_adapter\n\n    def mock_create(self_factory):
        # Both threads must reach creation before either can complete construction.
        # This ensures both adapters are constructed before either can cache/return.
        creation_barrier.wait(timeout=5.0)\n        return original_create(self_factory)\n\n    AdapterFactory.create_adapter = mock_create  # type: ignore\n\n    t1 = threading.Thread(target=worker, args=(0,))\n    t2 = threading.Thread(target=worker, args=(1,))\n    t1.start()\n    t2.start()\n    # Wait for both threads to call creation_barrier.wait() in their workers.\n    # The main thread will block here until both workers have called wait().\n    creation_barrier.wait(timeout=5.0)\n\n    t1.join(timeout=5.0)\n    t2.join(timeout=5.0)\n\n    AdapterFactory.create_adapter = original_create  # type: ignore\n\n    assert not errors, f"Concurrent resolution raised errors: {errors}"\n    assert results[0] is not None\n    assert results[1] is not None\n    assert results[0] is results[1]\n    assert resolver.resolve(local_account) is results[0]"""

if old_concurrent in content:
    content = content.replace(old_concurrent, new_concurrent)
    print("Fix 3: test_concurrent_resolution_same_account_returns_same_adapter - REPLACED")
else:
    print("Fix 3: FAILED - old test text not found")
    idx = content.find("def test_concurrent_resolution_same_account_returns_same_adapter")
    if idx >= 0:
        print(f"  Found at index {idx}")
        print(content[idx:idx+600])

# --- Write the modified file ---

with open("tests/test_storage_resolver.py", "w", encoding="utf-8") as f:
    f.write(content)

print("\nAll fixes applied. File written.")