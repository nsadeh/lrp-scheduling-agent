import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")

import sentry_sdk  # noqa: E402
from arq import create_pool  # noqa: E402
from arq.connections import RedisSettings  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from psycopg_pool import AsyncConnectionPool  # noqa: E402
from sentry_sdk.integrations.asyncio import AsyncioIntegration  # noqa: E402
from sentry_sdk.integrations.fastapi import FastApiIntegration  # noqa: E402

from api.addon.routes import addon_router, oauth_router, refresh_router  # noqa: E402
from api.ai import init_langfuse, init_llm_service  # noqa: E402
from api.classifier.hook import ClassifierHook  # noqa: E402
from api.classifier.service import SuggestionService  # noqa: E402
from api.drafts.service import DraftService  # noqa: E402
from api.gmail.auth import TokenStore  # noqa: E402
from api.gmail.client import GmailClient  # noqa: E402
from api.gmail.webhook import webhook_router  # noqa: E402
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

    gmail = getattr(app.state, "gmail", None)
    app.state.scheduling = LoopService(db_pool=pool, gmail=gmail)
    logger.info("LoopService initialized")

    # Redis for arq job queue (push pipeline)
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    try:
        redis = await create_pool(RedisSettings.from_dsn(redis_url))
        app.state.redis = redis
        logger.info("Redis pool connected for push pipeline")
    except Exception:
        app.state.redis = None
        logger.warning("Redis not available — push pipeline disabled, poll fallback only")

    # AI infrastructure — crashes on startup if not configured
    langfuse = init_langfuse()
    llm_service = init_llm_service()
    app.state.langfuse = langfuse
    app.state.llm_service = llm_service

    draft_service = DraftService(
        db_pool=pool,
        loop_service=app.state.scheduling,
        llm=llm_service,
        langfuse=langfuse,
    )
    app.state.draft_service = draft_service
    logger.info("DraftService initialized")

    app.state.email_hook = ClassifierHook(
        llm=llm_service,
        langfuse=langfuse,
        suggestion_service=SuggestionService(db_pool=pool),
        loop_service=app.state.scheduling,
        draft_service=draft_service,
    )
    logger.info("ClassifierHook active")

    yield

    # Cleanup — flush LangFuse traces before shutdown
    langfuse.flush()
    langfuse.shutdown()

    redis = getattr(app.state, "redis", None)
    if redis:
        await redis.close()
    await pool.close()


app = FastAPI(title="LRP Scheduling Agent", lifespan=lifespan)

# Static files (logo, etc.) — directory relative to working directory (services/api/)
static_dir = Path(__file__).resolve().parent.parent.parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(addon_router)
app.include_router(refresh_router)
app.include_router(oauth_router)
app.include_router(webhook_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
