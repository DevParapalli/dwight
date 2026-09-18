import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import progress
from app.db import init_db
from app.ui.routes import router as ui_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Stages run in threadpool workers; this is how their progress frames reach
    # the event loop without the SSE stream having to poll for them.
    progress.bind_loop(asyncio.get_running_loop())
    yield


app = FastAPI(title="dwight", lifespan=lifespan)
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "ui" / "static"),
    name="static",
)
app.include_router(ui_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
