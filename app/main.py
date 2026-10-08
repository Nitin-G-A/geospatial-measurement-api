"""FastAPI application entry point."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

import app.models  # noqa: F401  (registers tables on Base.metadata)
from app.database import Base, engine
from app.routers import files


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    yield


app = FastAPI(
    title="Geospatial File Measurement API",
    description=(
        "Upload a KML or zipped Shapefile and get per-feature area/length "
        "measurements computed in an appropriate projected CRS."
    ),
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(files.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
