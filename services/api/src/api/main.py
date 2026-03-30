import os
from contextlib import asynccontextmanager
from pathlib import Path

import sentry_sdk
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.fastapi import FastApiIntegration

from api.addon.routes import addon_router

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
    # Startup: init DB pool, Redis, etc.
    yield
    # Shutdown: close pools


app = FastAPI(title="LRP Scheduling Agent", lifespan=lifespan)

# Static files (logo, etc.) — directory relative to working directory (services/api/)
static_dir = Path(__file__).resolve().parent.parent.parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(addon_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
