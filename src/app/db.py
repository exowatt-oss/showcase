"""Synchronous SQLAlchemy database primitives and the Showcase worker boundary.

The application deliberately keeps the existing synchronous SQLAlchemy/psycopg
models, but every request/background database callable is submitted to the
bounded :class:`DatabaseRuntime`.  A cancelled await never cancels a running
sync callable: its ``WorkHandle`` remains tracked until the thread completes.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    LargeBinary,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.config import Settings, settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Presentation(Base):
    __tablename__ = "presentations"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    # SHA-256 hex of the uploader's owner token. NULL = legacy/ownerless page
    # (legacy rows only; public API requires a token).
    owner_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    html: Mapped[str] = mapped_column(Text)
    # Bytes of the HTML page, or of the latest version's bundle for a deck.
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    # "html" = single-file page served from ``html``; "deck" = deck bundle
    # (``html`` is empty, content lives in ``deck_versions`` + ``assets``).
    kind: Mapped[str] = mapped_column(String(16), default="html", server_default="html")
    latest_version: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)


class Asset(Base):
    """One deck-bundle file, stored once by the SHA-256 of its bytes."""

    __tablename__ = "assets"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    mime: Mapped[str] = mapped_column(String(100), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class DeckVersion(Base):
    """One immutable pushed version of a deck: its deck.json and path -> sha256 map."""

    __tablename__ = "deck_versions"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    deck_json: Mapped[str] = mapped_column(Text)
    files_json: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, default="")
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


def _postgres_options(config: Settings) -> str:
    """Build the fixed, validated libpq ``options`` value.

    Only integer settings are interpolated and all parameter names are fixed;
    callers cannot inject arbitrary PostgreSQL settings through the environment.
    """

    return (
        f"-c statement_timeout={config.DB_STATEMENT_TIMEOUT_MS} "
        f"-c lock_timeout={config.DB_LOCK_TIMEOUT_MS} "
        "-c idle_in_transaction_session_timeout="
        f"{config.DB_IDLE_IN_TRANSACTION_TIMEOUT_MS}"
    )


def _build_engine(url: str | None = None, config: Settings | None = None):
    """Build the engine with bounded PostgreSQL settings and a safe SQLite branch."""

    config = config or settings
    url = url or config.DATABASE_URL
    if url.startswith("sqlite"):
        # Single shared connection for in-memory DBs; allow cross-thread use
        # under TestClient / uvicorn workers. No PostgreSQL arguments here.
        from sqlalchemy.pool import StaticPool

        return create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)

    return create_engine(
        url,
        connect_args={
            "connect_timeout": int(config.DB_CONNECT_TIMEOUT_SECONDS),
            "options": _postgres_options(config),
        },
        pool_size=int(config.DB_POOL_SIZE),
        max_overflow=int(config.DB_MAX_OVERFLOW),
        pool_timeout=int(config.DB_POOL_TIMEOUT_SECONDS),
        pool_recycle=int(config.DB_POOL_RECYCLE_SECONDS),
        pool_pre_ping=True,
    )


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@dataclass(slots=True)
class TransactionResult[T]:
    value: T
    outcome: str
    operation_id: str


@dataclass(slots=True)
class Reconciliation[T]:
    value: T
    outcome: str  # committed or rolled_back


class RouteError(Exception):
    """Expected HTTP semantics raised inside a worker transaction."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class TransactionFailure(Exception):
    """A transaction failed with an explicit known/unknown outcome."""

    def __init__(
        self,
        operation_id: str,
        outcome: str,
        cause: BaseException,
        *,
        route_error: RouteError | None = None,
    ):
        super().__init__(type(cause).__name__)
        self.operation_id = operation_id
        self.outcome = outcome
        self.cause = cause
        self.route_error = route_error


class DatabaseStopping(RuntimeError):
    pass


class DatabaseBusy(RuntimeError):
    pass


class OperationTimedOut(asyncio.TimeoutError):
    def __init__(self, handle: WorkHandle):
        super().__init__(handle.operation)
        self.handle = handle


@dataclass(slots=True)
class WorkHandle[T]:
    operation_id: str
    operation: str
    future: Future[T]
    submitted_at: float
    timeout_seconds: float
    await_timed_out: bool = False

    @property
    def done(self) -> bool:
        return self.future.done()


def run_transaction[T](
    operation_id: str,
    body: Callable[[Any], T],
    *,
    reconcile: Callable[[], Reconciliation[T] | None] | None = None,
) -> TransactionResult[T]:
    """Run one complete transaction in one worker thread.

    The session never crosses the await boundary.  A commit exception is
    treated as unknown until an operation-specific reconciliation callback can
    prove that the natural-key side effect exists or does not exist.
    """

    db = SessionLocal()
    try:
        try:
            value = body(db)
        except RouteError as exc:
            try:
                db.rollback()
            except Exception:
                logger.warning(
                    "db_transaction operation=%s outcome=unknown rollback=error", operation_id
                )
                raise TransactionFailure(operation_id, "unknown", exc, route_error=exc) from exc
            raise TransactionFailure(operation_id, "rolled_back", exc, route_error=exc) from exc

        try:
            db.commit()
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                # A broken connection may make rollback impossible. The
                # commit result must remain unknown in that case.
                pass
            try:
                db.close()
            except Exception:
                pass
            if reconcile is not None:
                try:
                    reconciled = reconcile()
                except Exception:
                    reconciled = None
                if reconciled is not None:
                    if reconciled.outcome == "committed":
                        return TransactionResult(reconciled.value, "committed", operation_id)
                    raise TransactionFailure(operation_id, "rolled_back", exc) from exc
            raise TransactionFailure(operation_id, "unknown", exc) from exc

        return TransactionResult(value, "committed", operation_id)
    except TransactionFailure:
        raise
    except Exception as exc:
        try:
            db.rollback()
            outcome = "rolled_back"
        except Exception:
            outcome = "unknown"
        raise TransactionFailure(operation_id, outcome, exc) from exc
    finally:
        try:
            db.close()
        except Exception:
            # Closing is always attempted; a close failure must not take down
            # the cleanup/init supervisor or hide the original outcome.
            logger.warning("db_transaction operation=%s session_close=error", operation_id)


def init_db() -> None:
    """Create tables and run additive migrations in the initialization worker."""

    Base.metadata.create_all(engine)
    _migrate(engine)


def _migrate(db_engine) -> None:
    """Additive, idempotent column migrations for pre-existing deployments."""

    from sqlalchemy import inspect, text

    cols = {c["name"] for c in inspect(db_engine).get_columns("presentations")}
    if "owner_hash" not in cols:
        with db_engine.begin() as conn:
            conn.execute(text("ALTER TABLE presentations ADD COLUMN owner_hash VARCHAR(64)"))
    if "kind" not in cols:
        with db_engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE presentations ADD COLUMN kind VARCHAR(16) NOT NULL DEFAULT 'html'"
                )
            )
    if "latest_version" not in cols:
        with db_engine.begin() as conn:
            conn.execute(text("ALTER TABLE presentations ADD COLUMN latest_version INTEGER"))


class DatabaseRuntime:
    """Bounded executor, admission control, tracked futures, and shutdown drain."""

    def __init__(
        self,
        *,
        max_workers: int = settings.DB_MAX_WORKERS,
        operation_timeout: float = settings.DB_OPERATION_TIMEOUT_SECONDS,
        shutdown_timeout: float = settings.DB_SHUTDOWN_TIMEOUT_SECONDS,
    ):
        self.max_workers = max_workers
        self.operation_timeout = float(operation_timeout)
        self.shutdown_timeout = float(shutdown_timeout)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="showcase-db"
        )
        self._slots: asyncio.BoundedSemaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._handles: dict[str, WorkHandle[Any]] = {}
        self._futures: set[Future[Any]] = set()
        self._futures_lock = threading.Lock()
        self._stopping = False
        self._disposed = False
        self._deferred_dispose = False

    @property
    def stopping(self) -> bool:
        return self._stopping

    @property
    def in_flight(self) -> int:
        return len(self._handles)

    @property
    def tracked_futures(self) -> tuple[Future[Any], ...]:
        with self._futures_lock:
            return tuple(self._futures)

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._slots = asyncio.BoundedSemaphore(self.max_workers)

    async def submit(
        self,
        operation: str,
        callable_: Callable[[], T],
        *,
        operation_id: str,
        timeout: float | None = None,
    ) -> WorkHandle[T]:
        if self._stopping or self._slots is None or self._loop is None:
            raise DatabaseStopping("database runtime is stopping")
        timeout = self.operation_timeout if timeout is None else float(timeout)
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=timeout)
        except TimeoutError as exc:
            raise DatabaseBusy(operation) from exc
        if self._stopping:
            self._slots.release()
            raise DatabaseStopping("database runtime is stopping")
        started_at = time.monotonic()
        try:
            future = self._executor.submit(callable_)
        except Exception:
            self._slots.release()
            raise
        handle = WorkHandle(operation_id, operation, future, started_at, timeout)
        with self._futures_lock:
            self._futures.add(future)
        self._handles[operation_id] = handle
        future.add_done_callback(lambda done: self._schedule_finish(handle, done))
        return handle

    def _schedule_finish(self, handle: WorkHandle[Any], future: Future[Any]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            self._deferred_finish(handle, future)
            return
        try:
            loop.call_soon_threadsafe(self._finish, handle, future)
        except RuntimeError:
            self._deferred_finish(handle, future)

    def _finish(self, handle: WorkHandle[Any], future: Future[Any]) -> None:
        if self._handles.get(handle.operation_id) is not handle:
            return
        self._handles.pop(handle.operation_id, None)
        with self._futures_lock:
            self._futures.discard(future)
        if self._slots is not None:
            self._slots.release()
        try:
            result = future.result()
            outcome = getattr(result, "outcome", "completed")
        except TransactionFailure as exc:
            outcome = exc.outcome
        except Exception:
            outcome = "failed"
        timeout = "await" if handle.await_timed_out else "none"
        logger.info(
            "db_operation operation=%s operation_id=%s duration_ms=%.1f timeout=%s outcome=%s",
            handle.operation,
            handle.operation_id,
            (time.monotonic() - handle.submitted_at) * 1000,
            timeout,
            outcome,
        )
        if self._deferred_dispose and not self.tracked_futures:
            self._dispose_engine()
            self._deferred_dispose = False
            logger.info("db_shutdown outcome=deferred-drained in_flight=0")

    def _deferred_finish(self, handle: WorkHandle[Any], future: Future[Any]) -> None:
        with self._futures_lock:
            self._futures.discard(future)
        # The event loop is gone, so there is no semaphore or handle map to
        # update. This path exists only to make deferred engine disposal safe.
        self._maybe_deferred_dispose()

    async def wait(self, handle: WorkHandle[T], *, timeout: float | None = None) -> T:
        timeout = handle.timeout_seconds if timeout is None else float(timeout)
        wrapped = asyncio.wrap_future(handle.future)
        try:
            return await asyncio.wait_for(asyncio.shield(wrapped), timeout=timeout)
        except TimeoutError as exc:
            handle.await_timed_out = True
            raise OperationTimedOut(handle) from exc

    async def wait_for_completion(self, handle: WorkHandle[T]) -> T:
        """Await a submitted callable without imposing a second proxy timeout."""

        return await asyncio.shield(asyncio.wrap_future(handle.future))

    async def shutdown(self) -> None:
        """Stop admission, drain workers, and dispose only after all use ends."""

        self._stopping = True
        deadline = asyncio.get_running_loop().time() + self.shutdown_timeout
        while self._handles:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            handles = tuple(self._handles.values())
            group = asyncio.gather(
                *(asyncio.wrap_future(handle.future) for handle in handles),
                return_exceptions=True,
            )
            try:
                await asyncio.wait_for(asyncio.shield(group), timeout=remaining)
            except TimeoutError:
                break

        if self._handles:
            self._deferred_dispose = True
            for future in self.tracked_futures:
                future.add_done_callback(lambda _done: self._maybe_deferred_dispose())
            self._executor.shutdown(wait=False, cancel_futures=True)
            logger.warning(
                "db_shutdown outcome=deferred in_flight=%d timeout=%.1f",
                len(self._handles),
                self.shutdown_timeout,
            )
            return

        self._executor.shutdown(wait=True, cancel_futures=True)
        # Disposal closes synchronous SQLAlchemy connections; keep it off the
        # event loop even though all DB workers have already drained.
        await asyncio.to_thread(self._dispose_engine)
        logger.info("db_shutdown outcome=drained in_flight=0")

    def _maybe_deferred_dispose(self) -> None:
        if not self._deferred_dispose or self.tracked_futures:
            return
        self._dispose_engine()
        self._deferred_dispose = False
        logger.info("db_shutdown outcome=deferred-drained in_flight=0")

    def _dispose_engine(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        try:
            engine.dispose()
        except Exception:
            logger.exception("db_shutdown engine_dispose=error")
