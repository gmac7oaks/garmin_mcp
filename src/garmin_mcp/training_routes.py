"""Browser training dashboards: /training/ (index), /training/<page>, /training/data.json.

The pages are static HTML files in training_pages/ that fetch data.json from
the same origin. Every route needs the dashboard key, passed as ?key=<token>
(so the pages can be embedded as iframes, e.g. Home Assistant webpage cards)
or as "Authorization: Bearer <token>". The key is TRAINING_TOKEN, separate
from GARMIN_API_TOKEN because it ends up in page URLs.

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
from typing import Dict

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
}
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


def _authorised(request: Request, token: str) -> bool:
    supplied = request.query_params.get("key", "")
    if not supplied:
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        supplied = value if scheme.lower() == "bearer" else ""
    return bool(supplied) and hmac.compare_digest(supplied.strip().encode(), token.encode())


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex"
    return response


def register_training_routes(fastmcp, garmin_client, token: str, data: TrainingData = None) -> TrainingData:
    if data is None:
        data = TrainingData(garmin_client, config=config_from_env())

    denied = lambda: _no_store(JSONResponse({"error": "unauthorised"}, status_code=401))  # noqa: E731

    @fastmcp.custom_route("/training/data.json", methods=["GET"])
    async def training_data(request: Request) -> Response:
        if not _authorised(request, token):
            return denied()
        refresh = request.query_params.get("refresh") in ("1", "true", "yes")
        try:
            doc = await anyio.to_thread.run_sync(lambda: data.get(refresh=refresh))
        except Exception as exc:
            return _no_store(JSONResponse({"error": f"Garmin request failed: {exc}"}, status_code=502))
        return _no_store(JSONResponse(doc))

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
        if not _authorised(request, token):
            return denied()
        name = request.path_params.get("page", "").removesuffix(".html")
        if not _NAME.match(name) or name not in PAGES:
            return _no_store(JSONResponse({"error": "no such page"}, status_code=404))
        return _no_store(FileResponse(PAGES_DIR / f"{name}.html", media_type="text/html"))

    data.start_daily_refresh()
    return data
