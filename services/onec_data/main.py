import asyncio
from contextlib import asynccontextmanager
import secrets

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from tools import BackendError
from .client import ODataReader
from .config import DataSettings
from .models import BackendResult, DataCall
from .service import OneCDataService


def create_app(service: OneCDataService | None = None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.service = service or OneCDataService(ODataReader(DataSettings()))
        yield
        if service is None:
            await app.state.service.reader.close()

    app = FastAPI(title="Данные 1С", version="0.1.0", lifespan=lifespan)

    def check_token(x_internal_token: str | None = Header(default=None)):
        expected = app.state.service.settings.onec_data_token.get_secret_value()
        if expected and (not x_internal_token or not secrets.compare_digest(expected, x_internal_token)):
            raise HTTPException(status_code=401, detail="Неверный ключ внутреннего сервиса")

    @app.exception_handler(BackendError)
    async def data_error(_request, exc):
        return JSONResponse(status_code=400, content={"error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable}})

    @app.exception_handler(TimeoutError)
    async def timeout_error(_request, _exc):
        return JSONResponse(status_code=504, content={"error": {"code": "TIMEOUT", "message": "Истекло время запроса к 1С.", "retryable": True}})

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/v1/bootstrap", dependencies=[Depends(check_token)])
    async def bootstrap():
        async with asyncio.timeout(60):
            return await app.state.service.bootstrap()

    @app.post("/v1/call", response_model=BackendResult, dependencies=[Depends(check_token)])
    async def call(request: DataCall):
        async with asyncio.timeout(request.timeout):
            return await app.state.service.call(request)

    return app


app = create_app()
