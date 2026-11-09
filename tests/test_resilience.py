"""Deterministic lifecycle, worker-boundary, and transaction-outcome regressions."""

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
import app.main as main_module
from app.config import Settings
from app.db import DatabaseRuntime, TransactionResult, _build_engine
from app.main import app


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_postgres_engine_has_exact_bounds_and_sqlite_omits_them(monkeypatch):
    calls = []
    original = db_module.create_engine

    def capture(url, **kwargs):
        calls.append((url, kwargs))
        return object()

    monkeypatch.setattr(db_module, "create_engine", capture)
    config = Settings(
        DATABASE_URL="postgresql+psycopg://user:pass@localhost/db",
        DB_CONNECT_TIMEOUT_SECONDS=3,
        DB_STATEMENT_TIMEOUT_MS=5000,
        DB_LOCK_TIMEOUT_MS=1000,
        DB_IDLE_IN_TRANSACTION_TIMEOUT_MS=10000,
        DB_POOL_SIZE=5,
        DB_MAX_OVERFLOW=0,
        DB_POOL_TIMEOUT_SECONDS=2,
        DB_POOL_RECYCLE_SECONDS=1800,
    )
    _build_engine(config.DATABASE_URL, config)
    postgres_call = calls.pop()
    assert postgres_call[1]["connect_args"] == {
        "connect_timeout": 3,
        "options": (
            "-c statement_timeout=5000 -c lock_timeout=1000 "
            "-c idle_in_transaction_session_timeout=10000"
        ),
    }
    assert postgres_call[1]["pool_size"] == 5
    assert postgres_call[1]["max_overflow"] == 0
    assert postgres_call[1]["pool_timeout"] == 2
    assert postgres_call[1]["pool_recycle"] == 1800
    assert postgres_call[1]["pool_pre_ping"] is True

    _build_engine("sqlite://", config)
    sqlite_call = calls.pop()
    assert sqlite_call[1]["connect_args"] == {"check_same_thread": False}
    assert "pool_timeout" not in sqlite_call[1]
    assert "connect_timeout" not in sqlite_call[1]["connect_args"]
    monkeypatch.setattr(db_module, "create_engine", original)


def test_worker_await_timeout_is_contained_and_future_is_tracked():
    async def scenario():
        gate = threading.Event()
        runtime = DatabaseRuntime(max_workers=1, operation_timeout=0.03, shutdown_timeout=0.1)
        runtime.start()
        handle = await runtime.submit(
            "blocked_test",
            lambda: (gate.wait(2), "committed")[1],
            operation_id="blocked-operation",
        )
        with pytest.raises(db_module.OperationTimedOut):
            await runtime.wait(handle)
        assert runtime.in_flight == 1
        waiter = asyncio.create_task(runtime.wait(handle, timeout=1))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not handle.future.cancelled()
        gate.set()
        assert await runtime.wait_for_completion(handle) == "committed"
        await runtime.shutdown()
        assert runtime.in_flight == 0
        assert runtime.tracked_futures == ()

    asyncio.run(scenario())


def test_liveness_and_root_stay_responsive_while_init_worker_is_blocked(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    original_init = main_module.init_db

    def blocked_init():
        started.set()
        assert release.wait(2)

    monkeypatch.setattr(main_module, "init_db", blocked_init)
    try:
        with TestClient(app) as client:
            assert started.wait(1)
            started_at = time.monotonic()
            assert client.get("/api/health").json() == {"status": "ok"}
            assert client.get("/").status_code == 200
            assert time.monotonic() - started_at < 0.5
            ready = client.get("/api/ready")
            assert ready.status_code == 503
            assert ready.json() == {"status": "not_ready", "database": "unavailable"}
            assert ready.headers["Retry-After"] == "3"
            release.set()
    finally:
        monkeypatch.setattr(main_module, "init_db", original_init)


def test_db_down_startup_serves_liveness_and_gates_database_routes(monkeypatch):
    original_init = main_module.init_db

    def unavailable_init():
        raise OSError("simulated database outage")

    monkeypatch.setattr(main_module, "init_db", unavailable_init)
    try:
        with TestClient(app) as client:
            assert client.get("/").status_code == 200
            assert client.get("/api/health").json() == {"status": "ok"}
            assert client.get("/api/ready").status_code == 503
            listed = client.get("/api/presentations", headers={"X-Owner-Token": "owner"})
            assert listed.status_code == 503
            assert listed.json() == {"detail": "Database temporarily unavailable."}
    finally:
        monkeypatch.setattr(main_module, "init_db", original_init)


def test_upload_timeout_is_unknown_then_idempotency_reconciles(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    original_run = main_module.run_transaction
    original_timeout = main_module.settings.DB_OPERATION_TIMEOUT_SECONDS

    def blocked_upload(operation_id, body, **kwargs):
        if "upload_transaction" in body.__qualname__:
            started.set()
            assert release.wait(2)
        return original_run(operation_id, body, **kwargs)

    monkeypatch.setattr(main_module, "run_transaction", blocked_upload)
    monkeypatch.setattr(main_module.settings, "DB_OPERATION_TIMEOUT_SECONDS", 0.03)
    try:
        with TestClient(app) as client:
            headers = {
                "X-Owner-Token": "upload-timeout-owner",
                "X-Idempotency-Key": "upload-timeout-1",
            }
            file_data = {
                "file": ("timeout.html", b"<title>timeout upload</title>", "text/html"),
            }
            unknown = client.post(
                "/api/presentations",
                files=file_data,
                data={"slug": "timeout-upload"},
                headers=headers,
            )
            assert started.wait(1)
            assert unknown.status_code == 202
            assert unknown.json()["status"] == "unknown"
            release.set()
            reconciled = client.post(
                "/api/presentations",
                files=file_data,
                data={"slug": "timeout-upload"},
                headers=headers,
            )
            assert reconciled.status_code == 201
            assert reconciled.json()["slug"] == "timeout-upload"
            assert client.get("/timeout-upload").status_code == 200
    finally:
        monkeypatch.setattr(main_module.settings, "DB_OPERATION_TIMEOUT_SECONDS", original_timeout)
        monkeypatch.setattr(main_module, "run_transaction", original_run)


def test_shutdown_defers_engine_disposal_until_blocked_worker_finishes(monkeypatch):
    async def scenario():
        gate = threading.Event()
        disposed = threading.Event()
        original_dispose = db_module.engine.dispose

        def capture_dispose(*args, **kwargs):
            disposed.set()
            return original_dispose(*args, **kwargs)

        monkeypatch.setattr(db_module.engine, "dispose", capture_dispose)
        runtime = DatabaseRuntime(max_workers=1, operation_timeout=0.03, shutdown_timeout=0.03)
        runtime.start()
        handle = await runtime.submit(
            "database_cleanup",
            lambda: (gate.wait(2), TransactionResult(0, "committed", "cleanup"))[1],
            operation_id="cleanup-operation",
        )
        await runtime.shutdown()
        assert runtime.in_flight == 1
        assert not disposed.is_set()
        gate.set()
        assert await runtime.wait_for_completion(handle)
        assert _wait_until(disposed.is_set)
        assert runtime.tracked_futures == ()

    asyncio.run(scenario())


def test_lifespan_exits_without_tracked_work():
    with TestClient(app) as client:
        assert client.get("/api/ready").status_code in {200, 503}
    runtime = app.state.db_runtime
    assert runtime.in_flight == 0
    assert runtime.tracked_futures == ()
    assert app.state.db_lifecycle.status == "stopping"
