from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.api.router import api_router
from app.core.config import settings
from app.core.err import BizError, map_err, resp_json
from app.db.init_db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
    )
    app.include_router(api_router, prefix=settings.api_prefix)
    app.add_exception_handler(BizError, _on_err)
    app.add_exception_handler(RequestValidationError, _on_err)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"message": "OK"}

    return app


async def _on_err(request, exc):
    _, errcode, detail = map_err(exc)
    return resp_json(errcode, detail=detail)


app = create_app()
