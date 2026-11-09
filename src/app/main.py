import asyncio
import hashlib
import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from forwarded_auth import get_settings as auth_settings
from forwarded_auth.fastapi import require_user
from sqlalchemy import delete, select, text
from starlette.concurrency import run_in_threadpool

from app import decks
from app.config import settings
from app.db import (
    DatabaseBusy,
    DatabaseRuntime,
    DatabaseStopping,
    DeckVersion,
    OperationTimedOut,
    Presentation,
    Reconciliation,
    RouteError,
    SessionLocal,
    TransactionFailure,
    TransactionResult,
    init_db,
    run_transaction,
)
from app.slugs import RESERVED_SLUGS, extract_title, slugify, validate_slug

# --- Structured JSON logging ---
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(
    logging.Formatter(
        '{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}'
    )
)
logging.basicConfig(level=settings.LOG_LEVEL.upper(), handlers=[handler])
logger = logging.getLogger(__name__)

MAX_IDEMPOTENCY_KEY_LENGTH = 128
READY_GATE_WAIT_SECONDS = 1.0
UNKNOWN_RETRY_AFTER_SECONDS = "1"
NOT_READY_RETRY_AFTER_SECONDS = "3"


@dataclass(slots=True)
class LifecycleState:
    status: str = "initializing"
    stopping: bool = False
    ready_event: asyncio.Event = field(default_factory=asyncio.Event)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(slots=True)
class OperationRecord:
    key: str
    kind: str
    fingerprint: str
    operation_id: str
    handle: object | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    created_at: float = field(default_factory=time.monotonic)


class OperationLedger:
    """Process-local idempotency records retained while a result can reconcile."""

    def __init__(self) -> None:
        self._records: dict[str, OperationRecord] = {}
        self._ttl_seconds = max(float(settings.DB_OPERATION_TIMEOUT_SECONDS) * 2, 60.0)

    def begin(self, key: str | None, kind: str, fingerprint: str) -> tuple[OperationRecord, bool]:
        operation_id = uuid.uuid4().hex
        if key is None:
            return OperationRecord(operation_id, kind, fingerprint, operation_id), True
        now = time.monotonic()
        self._records = {
            record_key: record
            for record_key, record in self._records.items()
            if (
                now - record.created_at <= self._ttl_seconds
                or record.handle is not None
                and not getattr(record.handle, "done", True)
            )
        }
        existing = self._records.get(key)
        if existing is not None:
            if existing.kind != kind or existing.fingerprint != fingerprint:
                raise HTTPException(
                    409,
                    "X-Idempotency-Key was already used for a different operation.",
                )
            return existing, False
        record = OperationRecord(key, kind, fingerprint, operation_id)
        self._records[key] = record
        return record, True

    def forget(self, record: OperationRecord) -> None:
        if record.key:
            self._records.pop(record.key, None)


def _owner_hash(token: str) -> str | None:
    """SHA-256 hex of the caller's owner token; None when no token was sent."""
    token = token.strip()
    if not token:
        return None
    return hashlib.sha256(token.encode()).hexdigest()


def _validated_idempotency_key(value: str | None) -> str | None:
    key = (value or "").strip()
    if not key:
        return None
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise HTTPException(400, "X-Idempotency-Key must be at most 128 characters.")
    return key


def _may_modify(p: Presentation, caller_hash: str | None) -> bool:
    """Only the original owner token may modify a page; ownerless rows are locked."""
    return caller_hash is not None and p.owner_hash == caller_hash


def _generic_db_error(operation_id: str | None = None) -> HTTPException:
    headers = {"Retry-After": NOT_READY_RETRY_AFTER_SECONDS}
    if operation_id:
        headers["X-Operation-Id"] = operation_id
    return HTTPException(503, "Database temporarily unavailable.", headers=headers)


@dataclass(slots=True)
class UnknownWrite:
    operation_id: str


def _unknown_write_response(outcome: UnknownWrite) -> JSONResponse:
    return JSONResponse(
        status_code=202,
        content={"status": "unknown", "operation_id": outcome.operation_id},
        headers={
            "Retry-After": UNKNOWN_RETRY_AFTER_SECONDS,
            "X-Operation-Id": outcome.operation_id,
        },
    )


def _runtime(request: Request) -> DatabaseRuntime:
    runtime = getattr(request.app.state, "db_runtime", None)
    if runtime is None:
        raise _generic_db_error()
    return runtime


def _lifecycle(request: Request) -> LifecycleState:
    state = getattr(request.app.state, "db_lifecycle", None)
    if state is None:
        raise _generic_db_error()
    return state


async def _require_ready(request: Request) -> DatabaseRuntime:
    runtime = _runtime(request)
    state = _lifecycle(request)
    if state.status == "ready":
        return runtime
    if state.status in {"failed", "stopping"} or state.stopping:
        raise _generic_db_error()
    try:
        await asyncio.wait_for(state.ready_event.wait(), timeout=READY_GATE_WAIT_SECONDS)
    except TimeoutError as exc:
        raise _generic_db_error() from exc
    if state.status != "ready":
        raise _generic_db_error()
    return runtime


def _operation_fingerprint(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode())
        digest.update(b"\0")
    return digest.hexdigest()


async def _run_db(
    request: Request,
    *,
    operation: str,
    callable_,
    operation_id: str,
    idempotency_key: str | None = None,
    kind: str = "read",
    fingerprint: str = "",
    mutation: bool = False,
):
    runtime = await _require_ready(request)
    ledger: OperationLedger = request.app.state.operation_ledger
    try:
        record, created = ledger.begin(idempotency_key, kind, fingerprint)
    except HTTPException:
        raise
    if created:
        try:
            record.handle = await runtime.submit(
                operation,
                callable_,
                operation_id=record.operation_id,
            )
        except (DatabaseBusy, DatabaseStopping) as exc:
            ledger.forget(record)
            raise _generic_db_error(record.operation_id) from exc
    try:
        result = await runtime.wait(record.handle)
    except OperationTimedOut as exc:
        if mutation:
            return UnknownWrite(record.operation_id)
        raise _generic_db_error(record.operation_id) from exc
    except TransactionFailure as exc:
        if exc.route_error is not None:
            route = exc.route_error
            raise HTTPException(route.status_code, route.detail) from exc
        if exc.outcome == "unknown" and mutation:
            return UnknownWrite(record.operation_id)
        raise _generic_db_error(record.operation_id) from exc
    except Exception as exc:
        raise _generic_db_error(record.operation_id) from exc
    if not isinstance(result, TransactionResult):
        raise _generic_db_error(record.operation_id)
    if result.outcome == "unknown" and mutation:
        return UnknownWrite(record.operation_id)
    if result.outcome != "committed" and mutation:
        raise _generic_db_error(record.operation_id)
    return result


def _reconcile_upload(
    *,
    operation_id: str,
    candidate: str,
    explicit: bool,
    caller: str | None,
    html: str,
    size_bytes: int,
    title: str,
    replaced: bool,
) -> Reconciliation[dict] | None:
    """Prove whether a timed-out upload committed without replaying it."""

    with SessionLocal() as db:
        if explicit:
            row = db.get(Presentation, candidate)
            if row is None:
                return Reconciliation({}, "rolled_back")
            if row.owner_hash == caller and row.html == html and row.size_bytes == size_bytes:
                return Reconciliation(
                    {
                        "slug": row.slug,
                        "path": f"/{row.slug}",
                        "title": row.title,
                        "replaced": replaced,
                    },
                    "committed",
                )
            return None
        condition = (
            Presentation.owner_hash.is_(None)
            if caller is None
            else Presentation.owner_hash == caller
        )
        rows = db.execute(select(Presentation).where(condition)).scalars()
        for row in rows:
            if row.html == html and row.size_bytes == size_bytes and row.title == title:
                return Reconciliation(
                    {
                        "slug": row.slug,
                        "path": f"/{row.slug}",
                        "title": title,
                        "replaced": False,
                    },
                    "committed",
                )
        return Reconciliation({}, "rolled_back")


async def _initialization_owner(app: FastAPI) -> None:
    state: LifecycleState = app.state.db_lifecycle
    runtime: DatabaseRuntime = app.state.db_runtime
    while not state.stopping:
        state.status = "initializing"
        operation_id = uuid.uuid4().hex

        def initialize():
            return run_transaction(operation_id, lambda _db: init_db())

        handle = None
        try:
            handle = await runtime.submit("db_init", initialize, operation_id=operation_id)
            try:
                result = await runtime.wait(handle)
            except OperationTimedOut:
                logger.warning("db_init operation=%s timeout=await outcome=unknown", operation_id)
                result = await runtime.wait_for_completion(handle)
            if not isinstance(result, TransactionResult) or result.outcome != "committed":
                raise RuntimeError("initialization did not commit")
            state.status = "ready"
            state.ready_event.set()
            logger.info("db_lifecycle operation=%s outcome=ready", operation_id)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.status = "failed"
            logger.warning(
                "db_lifecycle operation=%s outcome=failed error=%s",
                operation_id,
                type(exc).__name__,
            )
            if state.stopping:
                return
            try:
                await asyncio.wait_for(
                    state.stop_event.wait(), timeout=settings.DB_INIT_RETRY_INTERVAL_SECONDS
                )
                return
            except TimeoutError:
                continue


@asynccontextmanager
async def lifespan(app: FastAPI):
    auth_settings().validate()  # Reject missing issuer/audience/allowlist before serving.
    logger.info("Starting up (environment=%s)", settings.ENVIRONMENT)
    runtime = DatabaseRuntime(
        max_workers=settings.DB_MAX_WORKERS,
        operation_timeout=settings.DB_OPERATION_TIMEOUT_SECONDS,
        shutdown_timeout=settings.DB_SHUTDOWN_TIMEOUT_SECONDS,
    )
    runtime.start()
    app.state.db_runtime = runtime
    app.state.db_lifecycle = LifecycleState()
    app.state.operation_ledger = OperationLedger()
    init_task = asyncio.create_task(_initialization_owner(app), name="showcase-db-init")
    # Let the initialization owner submit its worker before serving the first
    # request, while still yielding immediately when the database is unavailable.
    await asyncio.sleep(0)
    try:
        yield
    finally:
        state: LifecycleState = app.state.db_lifecycle
        state.stopping = True
        state.status = "stopping"
        state.stop_event.set()
        init_task.cancel()
        for task in (init_task,):
            with suppress(asyncio.CancelledError):
                await task
        await runtime.shutdown()
        logger.info("Shutting down")


app = FastAPI(
    title="Showcase",
    description=(
        "Instant hosting for single-file HTML pages — presentations, reports, demos. "
        "Upload a file, get a shareable URL on this domain immediately."
    ),
    lifespan=lifespan,
)

# --- CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Mutations always require a verified identity. The owner token is a *second* check
# for existing slugs, never a substitute for authentication. A proxy must strip
# client-supplied identity headers and block direct application access.
@app.middleware("http")
async def authenticate_writes(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        declared = request.headers.get("content-length")
        if declared and declared.isdecimal() and int(declared) > settings.MAX_REQUEST_BYTES:
            return JSONResponse(status_code=413, content={"detail": "Request too large"})
        try:
            await run_in_threadpool(require_user, request)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    response = await call_next(request)
    # Slug pages and bundle assets share a host with the upload UI. Always put
    # untrusted pages in an opaque origin, including SVG/HTML assets and pinned
    # decks. Without allow-same-origin they cannot read UI localStorage/cookies.
    slug = request.url.path.lstrip("/").split("/", 1)[0]
    if request.method in {"GET", "HEAD"} and slug and slug not in RESERVED_SLUGS:
        response.headers["Content-Security-Policy"] = "sandbox allow-scripts"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# --- Static assets and landing page ---
_app_dir = Path(__file__).parent
app.mount("/static", StaticFiles(directory=_app_dir / "static"), name="static")
templates = Jinja2Templates(directory=_app_dir / "templates")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request):
    """Upload page + list of hosted presentations."""
    response = templates.TemplateResponse(
        request,
        "index.html",
        {
            "app_title": app.title,
            "app_description": app.description or "Self-hosted HTML page and deck hosting.",
        },
    )
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return response


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(_app_dir / "static" / "favicon.svg", media_type="image/svg+xml")


# --- API routes ---
@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/ready")
async def ready(request: Request):
    """Bounded DB readiness; intentionally separate from process liveness."""
    state = getattr(request.app.state, "db_lifecycle", None)
    if state is None or state.status != "ready" or state.stopping:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "database": "unavailable"},
            headers={"Retry-After": NOT_READY_RETRY_AFTER_SECONDS},
        )
    operation_id = uuid.uuid4().hex

    def check_database():
        def body(db):
            db.execute(text("SELECT 1"))
            return True

        return run_transaction(operation_id, body)

    try:
        result = await _run_db(
            request,
            operation="db_ready",
            callable_=check_database,
            operation_id=operation_id,
        )
    except HTTPException:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "database": "unavailable"},
            headers={"Retry-After": NOT_READY_RETRY_AFTER_SECONDS},
        )
    if isinstance(result, UnknownWrite):
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "database": "unavailable"},
            headers={"Retry-After": NOT_READY_RETRY_AFTER_SECONDS},
        )
    return {"status": "ready", "database": "ok"}


@app.get("/api/presentations")
async def list_presentations(request: Request, x_owner_token: str = Header("")):
    """List only presentations uploaded with the caller's owner token."""
    caller = _owner_hash(x_owner_token)
    if caller is None:
        return []
    operation_id = uuid.uuid4().hex

    def list_transaction():
        def body(db):
            rows = db.execute(
                select(Presentation)
                .where(Presentation.owner_hash == caller)
                .order_by(Presentation.updated_at.desc())
            ).scalars()
            return [
                {
                    "slug": p.slug,
                    "title": p.title,
                    "path": f"/{p.slug}",
                    "size_bytes": p.size_bytes,
                    "created_at": p.created_at.isoformat(),
                    "updated_at": p.updated_at.isoformat(),
                    "owned": p.owner_hash is not None,
                    "mine": p.owner_hash == caller,
                    **_kind_fields(p.kind, p.latest_version),
                }
                for p in rows
            ]

        return run_transaction(operation_id, body)

    result = await _run_db(
        request,
        operation="presentation_list",
        callable_=list_transaction,
        operation_id=operation_id,
    )
    return result.value


@app.post("/api/presentations", status_code=201)
async def upload_presentation(
    request: Request,
    file: UploadFile = File(...),
    slug: str = Form(""),
    x_owner_token: str = Header(""),
    x_idempotency_key: str | None = Header(None),
):
    """Upload a single self-contained HTML file; it is served at /{slug} immediately."""
    filename = file.filename or "upload"
    if not filename.lower().endswith((".html", ".htm")):
        raise HTTPException(400, "Only .html files are accepted — upload a single HTML file.")

    raw = await file.read(settings.MAX_UPLOAD_BYTES + 1)
    if len(raw) > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File is larger than the configured MAX_UPLOAD_BYTES.")
    try:
        html = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            400, "File is not valid UTF-8 text — export the page as UTF-8 HTML."
        ) from exc

    title = extract_title(html)
    explicit = bool(slug.strip())
    base_candidate = slugify(slug) if explicit else (slugify(title) or slugify(Path(filename).stem))
    if not base_candidate:
        raise HTTPException(
            400, "Couldn't derive a slug — give the page a <title> or pass a slug explicitly."
        )
    if err := validate_slug(base_candidate):
        raise HTTPException(400, err)

    caller = _owner_hash(x_owner_token)
    if caller is None:
        raise HTTPException(400, "X-Owner-Token is required for uploads.")
    idempotency_key = _validated_idempotency_key(x_idempotency_key)
    fingerprint = _operation_fingerprint(
        "upload", caller or "", base_candidate, hashlib.sha256(raw).hexdigest()
    )
    operation_id = uuid.uuid4().hex

    upload_context = {"replaced": False}

    def upload_transaction():
        def body(db):
            candidate = base_candidate
            existing = db.get(Presentation, candidate)
            replaced = False
            if existing and explicit:
                if existing.kind == decks.KIND_DECK:
                    raise RouteError(
                        409,
                        f"/{candidate} is a deck bundle, not an HTML page — push new "
                        "versions with scripts/showcase-deck, or pick a different slug.",
                    )
                if not _may_modify(existing, caller):
                    raise RouteError(
                        403,
                        f"/{candidate} belongs to someone else — you can only update pages "
                        "you uploaded (from this browser). Pick a different slug.",
                    )
                existing.title = title or existing.title
                existing.html = html
                existing.size_bytes = len(raw)
                existing.owner_hash = caller
                replaced = True
            else:
                if existing:
                    base, n = candidate, 2
                    while db.get(Presentation, candidate) is not None:
                        suffix = f"-{n}"
                        candidate = base[: 64 - len(suffix)] + suffix
                        n += 1
                    if err := validate_slug(candidate):
                        raise RouteError(400, err)
                db.add(
                    Presentation(
                        slug=candidate,
                        title=title,
                        html=html,
                        size_bytes=len(raw),
                        owner_hash=caller,
                    )
                )
            upload_context["replaced"] = replaced
            return {
                "slug": candidate,
                "path": f"/{candidate}",
                "title": title,
                "replaced": replaced,
            }

        # Explicit slugs reconcile directly; derived slugs search the caller's
        # natural content key after a commit ambiguity.
        return run_transaction(
            operation_id,
            body,
            reconcile=lambda: _reconcile_upload(
                operation_id=operation_id,
                candidate=base_candidate,
                explicit=explicit,
                caller=caller,
                html=html,
                size_bytes=len(raw),
                title=title,
                replaced=bool(upload_context["replaced"]),
            ),
        )

    result = await _run_db(
        request,
        operation="presentation_upload",
        callable_=upload_transaction,
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        kind="upload",
        fingerprint=fingerprint,
        mutation=True,
    )
    if isinstance(result, UnknownWrite):
        return _unknown_write_response(result)
    value = result.value
    logger.info(
        "presentation_write operation=%s outcome=%s slug=%s size_bytes=%d",
        result.operation_id,
        result.outcome,
        value["slug"],
        len(raw),
    )
    return value


@app.delete("/api/presentations/{slug}", status_code=204)
async def delete_presentation(request: Request, slug: str, x_owner_token: str = Header("")):
    """Delete a page you own. Ownerless legacy pages cannot be modified."""
    caller = _owner_hash(x_owner_token)
    operation_id = uuid.uuid4().hex

    def delete_transaction():
        def body(db):
            p = db.get(Presentation, slug)
            if p is None:
                raise RouteError(404, f"No presentation at /{slug}.")
            if not _may_modify(p, caller):
                raise RouteError(
                    403,
                    f"/{slug} belongs to someone else — you can only delete pages "
                    "you uploaded (from this browser).",
                )
            # A deck's versions go with it; its assets stay (other decks may share them).
            db.execute(delete(DeckVersion).where(DeckVersion.slug == slug))
            db.delete(p)
            return None

        return run_transaction(operation_id, body)

    result = await _run_db(
        request,
        operation="presentation_delete",
        callable_=delete_transaction,
        operation_id=operation_id,
        mutation=True,
    )
    if isinstance(result, UnknownWrite):
        return _unknown_write_response(result)
    logger.info(
        "presentation_delete operation=%s outcome=%s slug=%s",
        operation_id,
        result.outcome,
        slug,
    )
    return None


# --- Public catalog (no upstream share-link service) ---
def _kind_fields(kind: str | None, latest_version: int | None) -> dict:
    if kind == decks.KIND_DECK:
        return {"kind": decks.KIND_DECK, "version": latest_version}
    return {"kind": decks.KIND_HTML}


@app.get("/api/library")
async def library(request: Request, x_owner_token: str = Header("")):
    """Public page/deck catalog, without page bodies or secret owner hashes."""
    caller = _owner_hash(x_owner_token)
    operation_id = uuid.uuid4().hex

    def fetch():
        def body(db):
            rows = db.execute(
                select(Presentation).order_by(Presentation.updated_at.desc())
            ).scalars()
            return [
                {
                    "slug": p.slug,
                    "title": p.title,
                    "path": f"/{p.slug}",
                    "size_bytes": p.size_bytes,
                    "created_at": p.created_at.isoformat(),
                    "updated_at": p.updated_at.isoformat(),
                    "mine": caller is not None and p.owner_hash == caller,
                    **_kind_fields(p.kind, p.latest_version),
                }
                for p in rows
            ]

        return run_transaction(operation_id, body)

    result = await _run_db(request, operation="library", callable_=fetch, operation_id=operation_id)
    return {"items": result.value}


# --- Deck bundles: API, pinned versions, assets, runtime (before the catch-all) ---
app.include_router(decks.router)


# --- Presentation serving (registered last: /docs, /static, /api/* all win first) ---
@app.api_route(
    "/{slug}", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False
)
async def serve_presentation(request: Request, slug: str):
    if slug in RESERVED_SLUGS:
        raise HTTPException(404, "Not found.")
    operation_id = uuid.uuid4().hex
    if_none_match = request.headers.get("if-none-match")

    def serve_transaction():
        def body(db):
            p = db.get(Presentation, slug)
            if p is None:
                raise RouteError(
                    404, f"No presentation at /{slug} — upload one from the home page."
                )
            if p.kind == decks.KIND_DECK:
                return decks.load_deck_page(db, p, None, if_none_match)
            return p.html

        # A deck is rendered after its transaction, still in the worker thread.
        return decks.finish(run_transaction(operation_id, body))

    result = await _run_db(
        request,
        operation="presentation_serve",
        callable_=serve_transaction,
        operation_id=operation_id,
    )
    if isinstance(result.value, decks.DeckPage):
        response = decks.page_response(result.value)
    else:
        response = HTMLResponse(result.value)
    return response
