#以下为lichlet冲突部分，全部保留
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from db import initdb
from err import BizError
from route import on_err, router


@asynccontextmanager
async def lifespan(app: FastAPI):
    initdb()
    yield


app = FastAPI(title="LKM-API", version="0.0.1", lifespan=lifespan)
app.include_router(router)
app.add_exception_handler(BizError, on_err)
app.add_exception_handler(RequestValidationError, on_err)
=======
from app.main import app
