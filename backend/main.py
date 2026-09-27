import logging
import os
import re
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from db import close_pool, init_pool
from repository import get_store, reset_store
from routers import (
    admin_router,
    api_router,
    auth_router,
    draws_router,
    lottery_router,
    stats_router,
    zodiac_router,
)
from services.auth import SESSION_SECRET_ENV, auth_enforced, is_production

load_dotenv()

logger = logging.getLogger(__name__)


DEFAULT_CORS_ORIGIN = "http://localhost:3000"


def _cors_config() -> tuple[list[str], str | None, bool]:
    """解析 CORS_ORIGINS，返回 (精确来源, 来源正则, allow_credentials)。

    - `*`：放行所有来源，并按 CORS 规范关闭 allow_credentials
      （浏览器不接受 `Access-Control-Allow-Origin: *` 与凭证同时出现）。
    - 含 `*` 的来源（如 `https://*.vercel.app`）：转成正则匹配，
      用于 Vercel Preview 这种每次部署都会变的子域名。
    """
    raw = os.getenv("CORS_ORIGINS", DEFAULT_CORS_ORIGIN)
    entries = [o.strip() for o in raw.split(",") if o.strip()] or [DEFAULT_CORS_ORIGIN]

    if "*" in entries:
        logger.warning(
            "CORS_ORIGINS contains '*': allowing all origins, "
            "allow_credentials is disabled (browsers reject '*' together with credentials)"
        )
        return ["*"], None, False

    exact = [o for o in entries if "*" not in o]
    patterns = [re.escape(o).replace(r"\*", ".*") for o in entries if "*" in o]
    origin_regex = "|".join(patterns) if patterns else None

    logger.info("CORS origins: %s (regex=%s)", exact, origin_regex)
    return exact, origin_regex, True


_cors_origins, _cors_origin_regex, _cors_credentials = _cors_config()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 认证灰度开关必须在启动日志里显眼可见（README / .env.example 同步说明）
    if auth_enforced():
        logger.warning(
            "[AUTH] AUTH_ENFORCED=ON：所有受保护接口按角色矩阵校验；"
            "前端必须先完成登录再调用，否则会出现 401/403。"
        )
        if not (os.getenv(SESSION_SECRET_ENV) or "").strip() and is_production():
            # session_secret() 本身也会抛错，这里提前把它暴露在启动阶段
            raise RuntimeError(
                f"{SESSION_SECRET_ENV} 未配置：生产环境开启 AUTH_ENFORCED 时必须配置会话签名密钥"
            )
    else:
        logger.warning(
            "[AUTH] AUTH_ENFORCED=OFF（显式旁路）：权限校验全部放行，"
            "所有接口（含 /api/admin/*）当前**无需登录**即可访问；"
            "生产环境请去掉该旁路或设为 AUTH_ENFORCED=true。"
        )

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
    allow_origins=_cors_origins,
    allow_origin_regex=_cors_origin_regex,
    allow_credentials=_cors_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(lottery_router)
app.include_router(draws_router)
app.include_router(zodiac_router)
app.include_router(stats_router)


@app.get("/")
async def root():
    return {"message": "Wave Money API", "docs": "/docs"}
