import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env", override=True)

import sentry_sdk  # noqa: E402
from arq import create_pool  # noqa: E402
from arq.connections import RedisSettings  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from psycopg_pool import AsyncConnectionPool  # noqa: E402
from sentry_sdk.integrations.asyncio import AsyncioIntegration  # noqa: E402
from sentry_sdk.integrations.fastapi import FastApiIntegration  # noqa: E402

from api.addon.routes import addon_router, oauth_router  # noqa: E402
from api.agent.service import AgentService  # noqa: E402
from api.agent.tracing import init_tracing  # noqa: E402
from api.agent.webhook import webhook_router  # noqa: E402
from api.gmail.auth import TokenStore  # noqa: E402
from api.gmail.client import GmailClient  # noqa: E402
from api.scheduling.service import LoopService  # noqa: E402

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
    # Initialise Langfuse tracing before creating any LLM clients
    init_tracing()

    database_url = os.environ.get("DATABASE_URL", "postgresql://dev:dev@localhost:5432/lrp_dev")
    pool = AsyncConnectionPool(conninfo=database_url)
    await pool.open()
    app.state.db = pool

    encryption_key = os.environ.get("GMAIL_TOKEN_ENCRYPTION_KEY", "")
    if encryption_key:
        token_store = TokenStore(db_pool=pool, encryption_key=encryption_key)
        app.state.token_store = token_store
        app.state.gmail = GmailClient(token_store)
        logger.info("GmailClient initialized with token store")
    else:
        logger.warning("GMAIL_TOKEN_ENCRYPTION_KEY not set — GmailClient not available")

    gmail = getattr(app.state, "gmail", None)
    app.state.scheduling = LoopService(db_pool=pool, gmail=gmail)
    app.state.agent_service = AgentService(db_pool=pool)
    logger.info("LoopService and AgentService initialized")

    # arq Redis pool for enqueuing background jobs
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    try:
        app.state.redis = await create_pool(RedisSettings.from_dsn(redis_url))
        logger.info("arq Redis pool created")
    except Exception:
        logger.warning("Failed to connect to Redis — background jobs unavailable", exc_info=True)
        app.state.redis = None

    yield

    # Shutdown
    redis_pool = getattr(app.state, "redis", None)
    if redis_pool is not None:
        await redis_pool.aclose()
    await pool.close()


app = FastAPI(title="LRP Scheduling Agent", lifespan=lifespan)

# Static files (logo, etc.) — directory relative to working directory (services/api/)
static_dir = Path(__file__).resolve().parent.parent.parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(addon_router)
app.include_router(oauth_router)
app.include_router(webhook_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
