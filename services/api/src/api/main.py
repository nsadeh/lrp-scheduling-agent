import os
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.fastapi import FastApiIntegration

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


@app.get("/health")
async def health():
    return {"status": "ok"}
