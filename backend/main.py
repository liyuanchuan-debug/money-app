import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from db import close_pool, init_pool
from repository import get_store, reset_store
from routers import api_router, lottery_router

load_dotenv()

logger = logging.getLogger(__name__)


def _parse_cors_origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS", "http://localhost:3000")
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    return origins or ["http://localhost:3000"]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        pool = await init_pool()
        if pool:
            logger.info("Database pool ready")
        else:
            logger.info("DATABASE_URL not set — falling back to in-memory store")
    except Exception:
        logger.exception("Database pool init failed — falling back to in-memory store")

    store = await get_store()
    logger.info("Active storage backend: %s", store.backend)

    yield

    await close_pool()
    reset_store()


app = FastAPI(
    title="Wave Money API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_parse_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
app.include_router(lottery_router)


@app.get("/")
async def root():
    return {"message": "Wave Money API", "docs": "/docs"}
