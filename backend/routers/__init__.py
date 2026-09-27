from .admin import router as admin_router
from .api import router as api_router
from .auth import router as auth_router
from .draws import router as draws_router
from .lottery import router as lottery_router
from .stats import router as stats_router
from .zodiac import router as zodiac_router

__all__ = [
    "admin_router",
    "api_router",
    "auth_router",
    "draws_router",
    "lottery_router",
    "stats_router",
    "zodiac_router",
]
