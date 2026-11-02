"""Plain HTTP endpoints served next to the MCP endpoint (HTTP transports only).

GET /api/nutrition
    The day's Garmin nutrition summary (goals, totals, remaining, % of goal and
    meals with their foods), plus optional daily history, as compact JSON for
    dashboards such as Home Assistant REST sensors.

    Query parameters:
        date  YYYY-MM-DD, default today (server time zone; pass it explicitly
              when the server runs in UTC and you don't)
        days  0-31, number of days of history ending on `date` (default 0)

GET /api/strength
    Strength-training report: this week's scheduled gym workouts with a
    weight suggestion per exercise, per-exercise load progression, weekly
    volume and a log of every session (see garmin_mcp.strength).

    Query parameters:
        date       YYYY-MM-DD, default today; sets "this week" (Mon-Sun)
        days       7-366, history length (default 120)
        log_parts  1-8, number of chunks the session log is split into, so
                   each fits a Home Assistant attribute (default 3)

Both routes require ``Authorization: Bearer <GARMIN_API_TOKEN>``. They are
only registered when GARMIN_API_TOKEN is set, because the MCP HTTP transport
itself has no authentication.
"""

import datetime
import hmac
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse

from garmin_mcp import strength

logger = logging.getLogger(__name__)

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


ACTIVITIES_URL = "/activitylist-service/activities/search/activities"
MAX_STRENGTH_DAYS = 366
_PAGE = 100


class _TTLCache:
    """Values with per-call freshness; entries older than `max_age` are dropped."""

    def __init__(self, clock: Callable[[], float], max_age: float = 8 * 86400):
        self._clock = clock
        self._max_age = max_age
        self._entries: Dict[Any, tuple] = {}
        self._lock = threading.Lock()
        self._swept = clock()

    def get(self, key, ttl: float, fetch: Callable[[], Any]):
        now = self._clock()
        with self._lock:
            hit = self._entries.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        value = fetch()
        with self._lock:
            self._entries[key] = (now, value)
            if now - self._swept > 3600:
                self._swept = now
                for stale in [k for k, v in self._entries.items() if now - v[0] > self._max_age]:
                    del self._entries[stale]
        return value


_SET_FIELDS = ("duration", "repetitionCount", "weight", "setType")


def _trim_sets(data: Optional[dict]) -> dict:
    """Keep only what the strength report reads (the raw response is ~60 KB)."""
    trimmed = []
    for raw in (data or {}).get("exerciseSets") or []:
        item = {field: raw.get(field) for field in _SET_FIELDS}
        item["exercises"] = [
            {"category": e.get("category"), "name": e.get("name"), "probability": e.get("probability")}
            for e in raw.get("exercises") or []
        ]
        trimmed.append(item)
    return {"activityId": (data or {}).get("activityId"), "exerciseSets": trimmed}


class StrengthData:
    """Fetches and caches the Garmin data behind /api/strength.

    A finished activity's sets rarely change after the first day or two, so
    they are kept for a week; the activity list, the week's schedule and the
    workout definitions are re-read every few minutes because they do change.
    """

    def __init__(
        self,
        client,
        clock: Callable[[], float] = time.monotonic,
        ttl_list: float = 10 * 60,
        ttl_recent_sets: float = 20 * 60,
        ttl_old_sets: float = 7 * 86400,
        ttl_plan: float = 15 * 60,
        ttl_report: float = 5 * 60,
    ):
        self._client = client
        self._cache = _TTLCache(clock)
        self._ttl_list = ttl_list
        self._ttl_recent_sets = ttl_recent_sets
        self._ttl_old_sets = ttl_old_sets
        self._ttl_plan = ttl_plan
        self._ttl_report = ttl_report
        self._build_lock = threading.Lock()

    def activities(self, start: datetime.date, end: datetime.date) -> list:
        def fetch():
            found, offset = [], 0
            while True:
                page = self._client.connectapi(
                    ACTIVITIES_URL,
                    params={
                        "startDate": start.isoformat(),
                        "endDate": end.isoformat(),
                        "start": str(offset),
                        "limit": str(_PAGE),
                        "activityType": "fitness_equipment",
                    },
                ) or []
                found.extend(page)
                if len(page) < _PAGE or offset >= 20 * _PAGE:
                    break
                offset += _PAGE
            return [
                a for a in found
                if ((a.get("activityType") or {}).get("typeKey")) == "strength_training"
            ]

        return self._cache.get(("activities", start, end), self._ttl_list, fetch)

    def exercise_sets(self, activity: dict, today: datetime.date):
        activity_id = activity.get("activityId")
        try:
            day = datetime.date.fromisoformat(str(activity.get("startTimeLocal"))[:10])
            settled = (today - day).days >= 2
        except ValueError:
            settled = False
        ttl = self._ttl_old_sets if settled else self._ttl_recent_sets
        return self._cache.get(
            ("sets", activity_id),
            ttl,
            lambda: _trim_sets(self._client.get_activity_exercise_sets(activity_id)),
        )

    def schedule(self, start: datetime.date, end: datetime.date) -> list:
        query = {
            "query": (
                "query{workoutScheduleSummariesScalar("
                f'startDate:"{start.isoformat()}", endDate:"{end.isoformat()}")}}'
            )
        }

        def fetch():
            result = self._client.query_garmin_graphql(query) or {}
            return (result.get("data") or {}).get("workoutScheduleSummariesScalar") or []

        return self._cache.get(("schedule", start, end), self._ttl_plan, fetch)

    def workout(self, workout_id) -> dict:
        return self._cache.get(
            ("workout", workout_id), self._ttl_plan, lambda: self._client.get_workout_by_id(workout_id)
        )

    def report(self, today: datetime.date, days: int, log_parts: int = 3) -> dict:
        # One build at a time, so overlapping polls don't double the Garmin calls.
        with self._build_lock:
            return self._cache.get(
                ("report", today, days, log_parts),
                self._ttl_report,
                lambda: self._build(today, days, log_parts),
            )

    def _build(self, today: datetime.date, days: int, log_parts: int) -> dict:
        since = today - datetime.timedelta(days=days)
        activities = self.activities(since, today)
        sets_by_id, failures = {}, []
        for activity in activities:
            try:
                sets_by_id[activity.get("activityId")] = self.exercise_sets(activity, today)
            except Exception as exc:  # one unreadable activity shouldn't sink the report
                logger.warning("Skipping activity %s: %s", activity.get("activityId"), exc)
                failures.append(exc)
        if activities and len(failures) == len(activities):
            raise failures[0]

        week_start = strength.monday(today)
        week_end = week_start + datetime.timedelta(days=6)
        schedule = [
            s for s in self.schedule(week_start, week_end)
            if (s.get("workoutType") or "strength_training") == "strength_training"
        ]
        workouts = {}
        for entry in schedule:
            workout_id = entry.get("workoutId")
            if workout_id and workout_id not in workouts:
                workouts[workout_id] = self.workout(workout_id)

        report = strength.build_report(
            activities, sets_by_id, schedule, workouts, today, days, log_parts=log_parts
        )
        report["summary"]["activities_skipped"] = len(failures)
        return report


def _authorised(request: Request, token: str) -> bool:
    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        return False
    return hmac.compare_digest(supplied.strip().encode(), token.encode())


def _query_int(request: Request, name: str, default: int, low: int, high: int) -> int:
    raw = request.query_params.get(name)
    value = int(raw) if raw not in (None, "") else default
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def register_routes(
    fastmcp,
    garmin_client,
    token: str,
    cache: Optional[FoodLogCache] = None,
    strength_data: Optional[StrengthData] = None,
):
    """Add /api/nutrition and /api/strength to the FastMCP app's HTTP server."""
    if cache is None:
        cache = FoodLogCache(
            lambda day: garmin_client.connectapi(f"/nutrition-service/food/logs/{day}")
        )
    if strength_data is None:
        strength_data = StrengthData(garmin_client)

    @fastmcp.custom_route("/api/strength", methods=["GET"])
    async def strength_report(request: Request) -> JSONResponse:
        if not _authorised(request, token):
            return JSONResponse({"error": "unauthorised"}, status_code=401)
        try:
            raw_date = request.query_params.get("date")
            day = datetime.date.fromisoformat(raw_date) if raw_date else datetime.date.today()
            days = _query_int(request, "days", 120, 7, MAX_STRENGTH_DAYS)
            log_parts = _query_int(request, "log_parts", 3, 1, 8)
        except ValueError as exc:
            message = (
                str(exc)
                if "between" in str(exc)
                else "date must be YYYY-MM-DD; days and log_parts whole numbers"
            )
            return JSONResponse({"error": message}, status_code=400)

        try:
            body = await anyio.to_thread.run_sync(lambda: strength_data.report(day, days, log_parts))
        except Exception as exc:  # Garmin errors surface as a 502 for the caller
            return JSONResponse({"error": f"Garmin request failed: {exc}"}, status_code=502)
        body = dict(body)
        body["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        return JSONResponse(body)

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
