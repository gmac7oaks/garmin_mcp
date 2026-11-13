"""Browser training dashboards: /training/ (index), /training/<page>, /training/data.json.

The pages are static HTML files in training_pages/ that fetch data.json from
the same origin. Every route needs the dashboard key, passed as ?key=<token>
(so the pages can be embedded as iframes, e.g. Home Assistant webpage cards)
or as "Authorization: Bearer <token>". The key is TRAINING_TOKEN, separate
from GARMIN_API_TOKEN because it ends up in page URLs.

TRAINING_SHARE_TOKEN (optional) is a second, view-only key for sharing with a
coach: it opens only the pages in SHARED_PAGES, and data.json answered for it
holds only SHARED_SECTIONS (no sleep, HRV, weight, food etc.). Change or remove
it to revoke access without touching TRAINING_TOKEN.

Optional settings shown on the pages (environment variables):
    TRAINING_GOAL_RACE   name of the goal race, e.g. "Marden 10K"
    TRAINING_GOAL_DATE   YYYY-MM-DD
    TRAINING_BLOCK_START YYYY-MM-DD, first day of the current training block
    TRAINING_BLOCK_LABELS  "YYYY-MM-DD=TAPER;YYYY-MM-DD=INJURY" (week starts)
"""

import hmac
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

import anyio
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response

from garmin_mcp.training_data import TrainingData

PAGES_DIR = Path(__file__).with_name("training_pages")
PAGES = {
    "commander": "The Commander",
    "tinkerer": "The Tinkerer",
    "brain": "The Brain",
    "coach": "The Coach",
    "racer": "The Racer",
    "recovery": "The Recovery Nerd",
    "archivist": "The Archivist",
    "minimalist": "The Minimalist",
    "raceweek": "Race Week",
    "fuel": "Fuel vs Training",
    "form": "Running Form",
    "planner": "Block Planner",
    "strength": "Strength Progression",
}
SHARED_PAGES = {"strength"}
SHARED_SECTIONS = ("strength", "date", "updated")
_NAME = re.compile(r"^[a-z]+$")


def config_from_env() -> Dict[str, object]:
    labels = {}
    for part in os.getenv("TRAINING_BLOCK_LABELS", "").split(";"):
        week, _, label = part.partition("=")
        if week.strip() and label.strip():
            labels[week.strip()] = label.strip()
    return {
        "goal_race": os.getenv("TRAINING_GOAL_RACE", "").strip() or None,
        "goal_date": os.getenv("TRAINING_GOAL_DATE", "").strip() or None,
        "block_start": os.getenv("TRAINING_BLOCK_START", "").strip() or None,
        "block_labels": labels,
    }


def _supplied_key(request: Request) -> str:
    supplied = request.query_params.get("key", "")
    if not supplied:
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        supplied = value if scheme.lower() == "bearer" else ""
    return supplied.strip()


def _matches(supplied: str, token: Optional[str]) -> bool:
    return bool(supplied) and bool(token) and hmac.compare_digest(supplied.encode(), token.encode())


def _authorised(request: Request, token: str) -> bool:
    return _matches(_supplied_key(request), token)


def _access(request: Request, token: str, share_token: Optional[str]) -> Optional[str]:
    """'owner' for the dashboard key, 'share' for the view-only key, else None."""
    supplied = _supplied_key(request)
    if _matches(supplied, token):
        return "owner"
    if _matches(supplied, share_token):
        return "share"
    return None


def shared_view(doc: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {name: doc.get(name) for name in SHARED_SECTIONS}
    out["activities"] = []
    out["errors"] = {k: v for k, v in (doc.get("errors") or {}).items() if k in SHARED_SECTIONS}
    out["shared"] = True
    return out


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex"
    return response


def register_training_routes(
    fastmcp, garmin_client, token: str, data: TrainingData = None, share_token: Optional[str] = None
) -> TrainingData:
    share_token = (share_token or "").strip() or None
    if share_token == token:
        raise ValueError("TRAINING_SHARE_TOKEN must differ from TRAINING_TOKEN")
    if data is None:
        data = TrainingData(garmin_client, config=config_from_env())

    denied = lambda: _no_store(JSONResponse({"error": "unauthorised"}, status_code=401))  # noqa: E731

    @fastmcp.custom_route("/training/data.json", methods=["GET"])
    async def training_data(request: Request) -> Response:
        access = _access(request, token, share_token)
        if access is None:
            return denied()
        # Only the owner can force a Garmin refresh.
        refresh = access == "owner" and request.query_params.get("refresh") in ("1", "true", "yes")
        try:
            doc = await anyio.to_thread.run_sync(lambda: data.get(refresh=refresh))
        except Exception as exc:
            return _no_store(JSONResponse({"error": f"Garmin request failed: {exc}"}, status_code=502))
        return _no_store(JSONResponse(doc if access == "owner" else shared_view(doc)))

    @fastmcp.custom_route("/training/common.js", methods=["GET"])
    async def common_js(request: Request) -> Response:
        return _no_store(FileResponse(PAGES_DIR / "common.js", media_type="application/javascript"))

    @fastmcp.custom_route("/training/", methods=["GET"])
    async def index(request: Request) -> Response:
        if not _authorised(request, token):
            return denied()
        key = request.query_params.get("key", "")
        links = "".join(
            f'<li><a href="/training/{name}?key={key}">{title}</a></li>' for name, title in PAGES.items()
        )
        return _no_store(
            HTMLResponse(
                "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                "<title>Training dashboards</title><body style='font:16px system-ui;padding:24px'>"
                f"<h1>Training dashboards</h1><ul>{links}</ul></body>"
            )
        )

    @fastmcp.custom_route("/training/{page}", methods=["GET"])
    async def page(request: Request) -> Response:
        access = _access(request, token, share_token)
        if access is None:
            return denied()
        name = request.path_params.get("page", "").removesuffix(".html")
        if access == "share" and name not in SHARED_PAGES:
            return denied()
        if not _NAME.match(name) or name not in PAGES:
            return _no_store(JSONResponse({"error": "no such page"}, status_code=404))
        return _no_store(FileResponse(PAGES_DIR / f"{name}.html", media_type="text/html"))

    data.start_daily_refresh()
    return data
