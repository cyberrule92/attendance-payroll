"""Application entrypoint.

Backend and frontend are served from one origin and one process: that keeps
CORS out of the picture, makes the Cloudflare Tunnel a single hostname, and
means the owner starts one thing on the laptop rather than two.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import create_all, engine
from .routers import admin, attendance, employees, kiosk, payroll, portal, records

STATIC_DIR = Path(__file__).resolve().parent / "static"

@asynccontextmanager
async def lifespan(_: FastAPI):
    create_all()
    yield
    engine.dispose()


app = FastAPI(
    title="Attendance & Payroll",
    version="1.0.0",
    description=(
        "Attendance capture for shop-floor kiosks and monthly payroll for the "
        "factory and stores."
    ),
    lifespan=lifespan,
)

app.include_router(kiosk.router)
app.include_router(admin.router)
app.include_router(employees.router)
app.include_router(attendance.router)
app.include_router(attendance.photo_router)
app.include_router(records.router)
app.include_router(payroll.router)
app.include_router(portal.router)


@app.get("/api/health")
def health() -> dict:
    """Used by the kiosk to decide whether to upload or keep queueing."""
    return {
        "ok": True,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "timezone": settings.timezone,
        "business_name": settings.business_name,
    }


# --- frontend ---------------------------------------------------------------

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _page(name: str) -> FileResponse:
    path = STATIC_DIR / name
    if not path.is_file():
        return JSONResponse(
            status_code=503,
            content={"detail": f"Frontend file {name} is missing from {STATIC_DIR}."},
        )
    # No-store: the shell is small, and a stale admin page after an update is
    # far more annoying than re-fetching 40 KB.
    return FileResponse(path, headers={"Cache-Control": "no-store"})


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/kiosk")


@app.get("/kiosk", include_in_schema=False)
def kiosk_page():
    return _page("kiosk.html")


@app.get("/admin", include_in_schema=False)
def admin_page():
    return _page("admin.html")


@app.get("/me", include_in_schema=False)
def portal_page():
    """The staff portal. Short path because employees type it on a phone."""
    return _page("portal.html")


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    """Must be served from the root path or it cannot control /kiosk."""
    path = STATIC_DIR / "sw.js"
    if not path.is_file():
        return JSONResponse(status_code=404, content={"detail": "No service worker."})
    return FileResponse(
        path,
        media_type="application/javascript",
        headers={"Cache-Control": "no-store", "Service-Worker-Allowed": "/"},
    )


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> JSONResponse:
    """Lets the kiosk be added to a tablet's home screen as an app."""
    return JSONResponse(
        {
            "name": f"{settings.business_name} Attendance",
            "short_name": "Attendance",
            "start_url": "/kiosk",
            "scope": "/",
            "display": "standalone",
            "background_color": "#0f172a",
            "theme_color": "#0f172a",
            "icons": [
                {
                    "src": "/static/icon.svg",
                    "sizes": "any",
                    "type": "image/svg+xml",
                    "purpose": "any maskable",
                }
            ],
        }
    )
