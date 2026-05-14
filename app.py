from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="UC Data Quality Explorer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from server.routes import catalog, duplicates, compare

app.include_router(catalog.router)
app.include_router(duplicates.router)
app.include_router(compare.router)

frontend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "dist")
index_html = os.path.join(frontend_dir, "index.html")

logger.info(f"Frontend dir: {frontend_dir}, exists: {os.path.exists(frontend_dir)}")
if os.path.exists(frontend_dir):
    logger.info(f"Frontend files: {os.listdir(frontend_dir)}")


@app.get("/")
async def serve_root():
    if os.path.exists(index_html):
        return FileResponse(index_html)
    return {"status": "running", "message": "UC Data Quality Explorer API", "docs": "/docs"}


if os.path.exists(frontend_dir):
    assets_dir = os.path.join(frontend_dir, "assets")
    if os.path.exists(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")


# SPA catch-all: serve index.html for any path not matched by API routes or
# static assets, allowing client-side (hash or history) routing to handle it.
@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    if os.path.exists(index_html):
        return FileResponse(index_html)
    return {"status": "running", "message": "UC Data Quality Explorer API", "docs": "/docs"}
