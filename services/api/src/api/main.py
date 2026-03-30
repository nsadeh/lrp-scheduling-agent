import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import sentry_sdk
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from psycopg_pool import AsyncConnectionPool
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.fastapi import FastApiIntegration

from api.addon.routes import addon_router
from api.gmail.auth import TokenStore
from api.gmail.client import GmailClient

logger = logging.getLogger(__name__)

sentry_sdk.init(
    dsn=os.environ.get("SENTRY_DSN"),
    environment=os.environ.get("ENVIRONMENT", "development"),
    traces_sample_rate=0.2,
    integrations=[
        FastApiIntegration(),
        AsyncioIntegration(),
    ],
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database_url = os.environ.get("DATABASE_URL", "postgresql://dev:dev@localhost:5432/lrp_dev")
    pool = AsyncConnectionPool(conninfo=database_url)
    await pool.open()
    app.state.db = pool

    encryption_key = os.environ.get("GMAIL_TOKEN_ENCRYPTION_KEY", "")
    if encryption_key:
        token_store = TokenStore(db_pool=pool, encryption_key=encryption_key)
        app.state.gmail = GmailClient(token_store)
        logger.info("GmailClient initialized with token store")
    else:
        logger.warning("GMAIL_TOKEN_ENCRYPTION_KEY not set — GmailClient not available")

    yield

    await pool.close()


app = FastAPI(title="LRP Scheduling Agent", lifespan=lifespan)

# Static files (logo, etc.) — directory relative to working directory (services/api/)
static_dir = Path(__file__).resolve().parent.parent.parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(addon_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
