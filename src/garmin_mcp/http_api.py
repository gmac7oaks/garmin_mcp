"""Plain HTTP endpoints served next to the MCP endpoint (HTTP transports only).

GET /api/nutrition
    The day's Garmin nutrition summary (goals, totals, remaining, % of goal and
    meals with their foods), plus optional daily history, as compact JSON for
    dashboards such as Home Assistant REST sensors.

    Query parameters:
        date  YYYY-MM-DD, default today (server time zone; pass it explicitly
              when the server runs in UTC and you don't)
        days  0-31, number of days of history ending on `date` (default 0)

    Requires ``Authorization: Bearer <GARMIN_API_TOKEN>``. The route is only
    registered when GARMIN_API_TOKEN is set, because the MCP HTTP transport
    itself has no authentication.
"""

import datetime
import hmac
import threading
import time
from typing import Any, Callable, Optional

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse

# Garmin's names for the daily goal fields we report, keyed by our names.
_GOAL_KEYS = {
    "calories": "calories",
    "protein": "protein",
    "carbs": "carbs",
    "fat": "fat",
    "fiber": "fiber",
    "saturated_fat": "saturatedFat",
    "added_sugars": "addedSugars",
}
_TOTAL_KEYS = {
    "calories": "calories",
    "protein": "protein",
    "carbs": "carbs",
    "fat": "fat",
    "fiber": "fiber",
    "sugar": "sugar",
    "saturated_fat": "saturatedFat",
    "sodium": "sodium",
}
_MACROS = ("calories", "protein", "carbs", "fat")
MAX_HISTORY_DAYS = 31


def _num(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _amount(value: Optional[float], key: str) -> Optional[float]:
    """Calories and sodium as whole numbers, grams to one decimal place."""
    if value is None:
        return None
    if key in ("calories", "sodium"):
        return int(round(value))
    return round(value, 1)


def summarise_food_log(data: dict) -> dict:
    """Condense a /nutrition-service/food/logs/{date} response."""
    goals_src = data.get("dailyNutritionGoals") or {}
    content = data.get("dailyNutritionContent") or {}

    goals = {k: _amount(_num(goals_src.get(src)), k) for k, src in _GOAL_KEYS.items()}
    totals = {k: _amount(_num(content.get(src)) or 0.0, k) for k, src in _TOTAL_KEYS.items()}
    remaining = {
        k: _amount(goals[k] - totals[k], k) if goals.get(k) is not None else None
        for k in _MACROS
    }
    percent = {
        k: int(round(totals[k] / goals[k] * 100)) if goals.get(k) else None
        for k in _MACROS
    }

    meals = []
    for detail in data.get("mealDetails") or []:
        meal = detail.get("meal") or {}
        meal_content = detail.get("mealNutritionContent") or {}
        meal_goals = detail.get("mealNutritionGoals") or meal.get("goals") or {}
        foods = []
        for logged in detail.get("loggedFoods") or []:
            meta = logged.get("foodMetaData") or {}
            per_serving = logged.get("nutritionContent") or {}
            qty = _num(logged.get("servingQty")) or 1.0
            foods.append(
                {
                    "name": meta.get("foodName") or logged.get("name") or "Quick add",
                    "time": logged.get("mealTime"),
                    "servings": qty,
                    **{
                        k: _amount((_num(per_serving.get(k)) or 0.0) * qty, k)
                        for k in _MACROS
                    },
                }
            )
        meals.append(
            {
                "name": str(meal.get("mealName") or "").replace("_", " ").title(),
                **{k: _amount(_num(meal_content.get(k)) or 0.0, k) for k in _MACROS},
                "goal_calories": _amount(_num(meal_goals.get("calories")), "calories"),
                "foods": foods,
            }
        )

    return {
        "date": data.get("mealDate"),
        "goals": goals,
        "totals": totals,
        "remaining": remaining,
        "percent": percent,
        "meals": meals,
    }


class FoodLogCache:
    """Per-day summaries with a freshness window that grows with the day's age.

    Today changes as food is logged, yesterday is often tidied up the next
    morning, and older days rarely change, so history requests don't cost a
    Garmin call per day on every poll.
    """

    def __init__(
        self,
        fetch: Callable[[str], dict],
        clock: Callable[[], float] = time.monotonic,
        ttl_today: float = 60,
        ttl_yesterday: float = 30 * 60,
        ttl_older: float = 6 * 3600,
    ):
        self._fetch = fetch
        self._clock = clock
        self._ttls = (ttl_today, ttl_yesterday, ttl_older)
        self._entries: dict = {}
        self._lock = threading.Lock()

    def _ttl(self, day: datetime.date, today: datetime.date) -> float:
        age = (today - day).days
        if age <= 0:
            return self._ttls[0]
        if age == 1:
            return self._ttls[1]
        return self._ttls[2]

    def get(self, day: datetime.date, today: datetime.date) -> dict:
        now = self._clock()
        with self._lock:
            hit = self._entries.get(day)
            if hit and now - hit[0] < self._ttl(day, today):
                return hit[1]
        summary = summarise_food_log(self._fetch(day.isoformat()) or {})
        with self._lock:
            self._entries[day] = (now, summary)
        return summary


def _authorised(request: Request, token: str) -> bool:
    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        return False
    return hmac.compare_digest(supplied.strip().encode(), token.encode())


def register_routes(fastmcp, garmin_client, token: str, cache: Optional[FoodLogCache] = None):
    """Add /api/nutrition to the FastMCP app's HTTP server."""
    if cache is None:
        cache = FoodLogCache(
            lambda day: garmin_client.connectapi(f"/nutrition-service/food/logs/{day}")
        )

    @fastmcp.custom_route("/api/nutrition", methods=["GET"])
    async def nutrition(request: Request) -> JSONResponse:
        if not _authorised(request, token):
            return JSONResponse({"error": "unauthorised"}, status_code=401)

        today = datetime.date.today()
        try:
            raw_date = request.query_params.get("date")
            day = datetime.date.fromisoformat(raw_date) if raw_date else today
            days = int(request.query_params.get("days", "0"))
        except ValueError:
            return JSONResponse(
                {"error": "date must be YYYY-MM-DD and days a whole number"},
                status_code=400,
            )
        if not 0 <= days <= MAX_HISTORY_DAYS:
            return JSONResponse(
                {"error": f"days must be between 0 and {MAX_HISTORY_DAYS}"},
                status_code=400,
            )
        # The caller's "today" (from `date`) decides how fresh each day must be.
        reference = max(day, today)

        def build() -> dict:
            summary = dict(cache.get(day, reference))
            history = []
            for offset in range(days - 1, -1, -1):
                past = day - datetime.timedelta(days=offset)
                past_summary = summary if offset == 0 else cache.get(past, reference)
                history.append(
                    {
                        "date": past.isoformat(),
                        **{k: past_summary["totals"][k] for k in _MACROS},
                        **{f"goal_{k}": past_summary["goals"][k] for k in _MACROS},
                    }
                )
            summary["history"] = history
            summary["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat(
                timespec="seconds"
            )
            return summary

        try:
            body = await anyio.to_thread.run_sync(build)
        except Exception as exc:  # Garmin errors surface as a 502 for the caller
            return JSONResponse({"error": f"Garmin request failed: {exc}"}, status_code=502)
        return JSONResponse(body)

    return nutrition
