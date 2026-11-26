import asyncio
from contextlib import asynccontextmanager, suppress
import secrets
import re
import time
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from tools import BackendError
from .client import ODataReader
from .config import DataSettings
from .models import BackendResult, DataCall
from .service import OneCDataService
from .observability import configure_logging, log_event, request_scope


def create_app(service: OneCDataService | None = None):
    @asynccontextmanager
    async def lifespan(app):
        configure_logging()
        app.state.service = service or OneCDataService(ODataReader(DataSettings()))
        interval = app.state.service.settings.onec_metadata_refresh_seconds
        task = asyncio.create_task(app.state.service.refresh_metadata_loop()) if interval > 0 else None
        app.state.metadata_refresh_task = task
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if service is None:
                await app.state.service.reader.close()

    app = FastAPI(title="Данные 1С", version="0.2.1", lifespan=lifespan)

    @app.middleware("http")
    async def trace_request(request: Request, call_next):
        incoming = request.headers.get("X-Request-ID", "")
        request.state.request_id = incoming if re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", incoming) else uuid4().hex
        started = time.monotonic()
        with request_scope(request.state.request_id, 60):
            response = await call_next(request)
            response.headers["X-Request-ID"] = request.state.request_id
            log_event("data_request", request_id=request.state.request_id, path=request.url.path,
                      status=response.status_code, duration_ms=round((time.monotonic() - started) * 1000, 3))
            return response

    def check_token(x_internal_token: str | None = Header(default=None)):
        expected = app.state.service.settings.onec_data_token.get_secret_value()
        if expected and (not x_internal_token or not secrets.compare_digest(expected, x_internal_token)):
            raise HTTPException(status_code=401, detail="Неверный ключ внутреннего сервиса")

    @app.exception_handler(BackendError)
    async def data_error(request, exc):
        status = {"TIMEOUT": 504, "API_UNAVAILABLE": 503, "ODATA_AUTH": 502, "ODATA_ERROR": 502,
                  "INVALID_API_RESPONSE": 502, "ACCESS_DENIED": 403, "METADATA_STATE_ERROR": 500}.get(exc.code, 400)
        return JSONResponse(status_code=status, content={"error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable,
            "details": {**exc.details, "request_id": request.state.request_id}}})

    @app.exception_handler(TimeoutError)
    async def timeout_error(request, _exc):
        return JSONResponse(status_code=504, content={"error": {"code": "TIMEOUT", "message": "Истекло время запроса к 1С.", "retryable": True,
            "details": {"stage": "deadline", "error_type": "DeadlineExceeded", "request_id": request.state.request_id}}})

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": app.version}

    @app.get("/v1/bootstrap", dependencies=[Depends(check_token)])
    async def bootstrap(refresh: bool = False):
        async with asyncio.timeout(60):
            if refresh:
                await app.state.service.snapshot(force_refresh=True)
            return await app.state.service.bootstrap()

    @app.post("/v1/call", response_model=BackendResult, dependencies=[Depends(check_token)])
    async def call(body: DataCall, request: Request):
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", body.context.request_id):
            request.state.request_id = body.context.request_id
        with request_scope(request.state.request_id, body.timeout):
            async with asyncio.timeout(body.timeout):
                return await app.state.service.call(body)

    return app


app = create_app()
