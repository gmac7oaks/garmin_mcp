"""Tests for the plain HTTP API (GET /api/nutrition and /api/strength)."""
import datetime
import time

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from garmin_mcp import http_api
from tests.fixtures.strength_data import ACTIVITIES as STRENGTH_ACTIVITIES
from tests.fixtures.strength_data import SCHEDULE as STRENGTH_SCHEDULE
from tests.fixtures.strength_data import WORKOUTS as STRENGTH_WORKOUTS
from tests.fixtures.strength_data import load_sets

TOKEN = "s3cret-token"

# Trimmed GET /nutrition-service/food/logs/{date} response.
FOOD_LOG = {
    "mealDate": "2026-11-01",
    "dailyNutritionGoals": {
        "calories": 2300,
        "carbs": 225.0,
        "fat": 80.0,
        "protein": 170.0,
        "fiber": 30.0,
        "addedSugars": 36.0,
        "saturatedFat": 22.0,
    },
    "dailyNutritionContent": {
        "calories": 3410,
        "carbs": 364.0,
        "fat": 153.0,
        "protein": 124.0,
        "fiber": 9.0,
        "sugar": 12.0,
        "saturatedFat": 36.0,
        "sodium": 1266.0,
    },
    "mealDetails": [
        {
            "meal": {"mealName": "BREAKFAST", "goals": {"calories": 414}},
            "mealNutritionContent": {"calories": 547, "carbs": 71.0, "protein": 36.0, "fat": 12.0},
            "mealNutritionGoals": {"calories": 414},
            "loggedFoods": [
                {
                    "servingQty": 0.5,
                    "mealTime": "08:03:00",
                    "foodMetaData": {"foodName": "Blueberries"},
                    "nutritionContent": {"calories": 57, "carbs": 12.0, "protein": 0.7, "fat": 0.3},
                },
                {
                    "servingQty": 1.0,
                    "mealTime": "08:01:00",
                    "foodMetaData": {"foodName": "Salted peanut caramel protein powder"},
                    "nutritionContent": {"calories": 106, "carbs": 1.1, "protein": 24.0, "fat": 0.5},
                },
            ],
        },
        {
            "meal": {"mealName": "DINNER", "goals": {"calories": 1081}},
            "mealNutritionGoals": {"calories": 1081},
            "loggedFoods": [],
        },
    ],
}

EMPTY_DAY = {
    "mealDate": "2026-11-02",
    "dailyNutritionGoals": FOOD_LOG["dailyNutritionGoals"],
    "mealDetails": [],
}


def test_summarise_totals_goals_and_percent():
    s = http_api.summarise_food_log(FOOD_LOG)
    assert s["date"] == "2026-11-01"
    assert s["goals"]["calories"] == 2300
    assert s["goals"]["protein"] == 170.0
    assert s["goals"]["saturated_fat"] == 22.0
    assert s["totals"]["calories"] == 3410
    assert s["totals"]["saturated_fat"] == 36.0
    assert s["remaining"] == {"calories": -1110, "protein": 46.0, "carbs": -139.0, "fat": -73.0}
    assert s["percent"] == {"calories": 148, "protein": 73, "carbs": 162, "fat": 191}


def test_summarise_meals_scale_foods_by_servings():
    s = http_api.summarise_food_log(FOOD_LOG)
    breakfast, dinner = s["meals"]
    assert breakfast["name"] == "Breakfast"
    assert breakfast["calories"] == 547
    assert breakfast["goal_calories"] == 414
    berries = breakfast["foods"][0]
    assert berries["name"] == "Blueberries"
    assert berries["servings"] == 0.5
    assert berries["calories"] == 28  # 57 kcal x 0.5, rounded
    assert berries["protein"] == 0.3  # 0.35 g rounds to one decimal place
    assert dinner == {
        "name": "Dinner",
        "calories": 0,
        "protein": 0.0,
        "carbs": 0.0,
        "fat": 0.0,
        "goal_calories": 1081,
        "foods": [],
    }


def test_summarise_empty_day_reports_zeros():
    s = http_api.summarise_food_log(EMPTY_DAY)
    assert s["totals"]["calories"] == 0
    assert s["totals"]["protein"] == 0.0
    assert s["remaining"]["calories"] == 2300
    assert s["percent"]["protein"] == 0
    assert s["meals"] == []


def test_summarise_without_goals_leaves_them_unset():
    s = http_api.summarise_food_log({"mealDate": "2026-11-02"})
    assert s["goals"]["calories"] is None
    assert s["remaining"]["calories"] is None
    assert s["percent"]["calories"] is None


def test_cache_freshness_depends_on_the_days_age():
    calls = []
    now = [1000.0]
    cache = http_api.FoodLogCache(
        lambda day: calls.append(day) or {"mealDate": day},
        clock=lambda: now[0],
        ttl_today=60,
        ttl_yesterday=1800,
        ttl_older=21600,
    )
    today = datetime.date(2026, 11, 2)
    yesterday = datetime.date(2026, 11, 1)
    older = datetime.date(2026, 10, 25)
    for day in (today, yesterday, older):
        cache.get(day, today)
    assert calls == ["2026-11-02", "2026-11-01", "2026-10-25"]

    now[0] += 120  # past today's window only
    for day in (today, yesterday, older):
        cache.get(day, today)
    assert calls[3:] == ["2026-11-02"]

    now[0] += 3600  # past yesterday's window too
    for day in (today, yesterday, older):
        cache.get(day, today)
    assert calls[4:] == ["2026-11-02", "2026-11-01"]


class _FakeClient:
    def __init__(self, logs):
        self.logs = logs
        self.urls = []

    def connectapi(self, url):
        self.urls.append(url)
        day = url.rsplit("/", 1)[-1]
        return self.logs.get(day, {"mealDate": day, "dailyNutritionGoals": FOOD_LOG["dailyNutritionGoals"]})


@pytest.fixture
def client_and_garmin():
    garmin = _FakeClient({"2026-11-01": FOOD_LOG, "2026-11-02": EMPTY_DAY})
    fastmcp = FastMCP("test")
    http_api.register_routes(fastmcp, garmin, TOKEN)
    return TestClient(fastmcp.streamable_http_app()), garmin


def test_route_requires_bearer_token(client_and_garmin):
    client, garmin = client_and_garmin
    assert client.get("/api/nutrition?date=2026-11-01").status_code == 401
    bad = client.get("/api/nutrition?date=2026-11-01", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    assert garmin.urls == []


def test_route_returns_summary_and_history(client_and_garmin):
    client, garmin = client_and_garmin
    r = client.get(
        "/api/nutrition?date=2026-11-02&days=3",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == "2026-11-02"
    assert body["totals"]["calories"] == 0
    assert [h["date"] for h in body["history"]] == ["2026-10-31", "2026-11-01", "2026-11-02"]
    assert body["history"][1]["calories"] == 3410
    assert body["history"][1]["goal_protein"] == 170.0
    assert "updated" in body
    assert garmin.urls == [
        "/nutrition-service/food/logs/2026-11-02",
        "/nutrition-service/food/logs/2026-10-31",
        "/nutrition-service/food/logs/2026-11-01",
    ]


def test_route_rejects_bad_parameters(client_and_garmin):
    client, _ = client_and_garmin
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/api/nutrition?date=02-11-2026", headers=auth).status_code == 400
    assert client.get("/api/nutrition?days=40", headers=auth).status_code == 400
    assert client.get("/api/nutrition?days=x", headers=auth).status_code == 400


def test_route_reports_garmin_failure_as_502():
    class Broken:
        def connectapi(self, url):
            raise RuntimeError("rate limited")

    fastmcp = FastMCP("test")
    http_api.register_routes(fastmcp, Broken(), TOKEN)
    client = TestClient(fastmcp.streamable_http_app())
    r = client.get("/api/nutrition?date=2026-11-02", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 502
    assert "rate limited" in r.json()["error"]


# --- /api/strength -------------------------------------------------------

AUTH = {"Authorization": f"Bearer {TOKEN}"}


class _FakeStrengthClient:
    def __init__(self, fail_sets=(), fail_list=False):
        self.calls = []
        self.sets = load_sets()
        self.fail_sets = set(fail_sets)
        self.fail_list = fail_list

    def connectapi(self, url, params=None):
        self.calls.append(("list", url, dict(params or {})))
        if self.fail_list:
            raise RuntimeError("rate limited")
        return list(STRENGTH_ACTIVITIES) if (params or {}).get("start") == "0" else []

    def get_activity_exercise_sets(self, activity_id):
        self.calls.append(("sets", activity_id))
        if activity_id in self.fail_sets:
            raise RuntimeError("not found")
        return self.sets[activity_id]

    def query_garmin_graphql(self, query):
        self.calls.append(("schedule", query["query"]))
        return {"data": {"workoutScheduleSummariesScalar": STRENGTH_SCHEDULE}}

    def get_workout_by_id(self, workout_id):
        self.calls.append(("workout", workout_id))
        return STRENGTH_WORKOUTS[workout_id]


def _strength_app(garmin, now=None):
    fastmcp = FastMCP("test")
    data = http_api.StrengthData(garmin, clock=(lambda: now[0]) if now else time.monotonic)
    http_api.register_routes(fastmcp, garmin, TOKEN, strength_data=data)
    return TestClient(fastmcp.streamable_http_app())


def test_strength_route_requires_bearer_token():
    garmin = _FakeStrengthClient()
    client = _strength_app(garmin)
    assert client.get("/api/strength").status_code == 401
    assert garmin.calls == []


def test_strength_route_returns_the_report():
    garmin = _FakeStrengthClient()
    r = _strength_app(garmin).get("/api/strength?date=2026-11-02", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["increases_due"] == 5
    assert body["summary"]["sessions"] == 4
    assert body["summary"]["activities_skipped"] == 0
    assert [w["workout"] for w in body["this_week"]] == ["Push v3", "Legs v2"]
    assert body["progress"]["push"]["exercises"][0]["name"] == "Incline dumbbell bench press"
    assert "updated" in body

    kind, url, params = garmin.calls[0]
    assert (kind, url) == ("list", http_api.ACTIVITIES_URL)
    assert params["startDate"] == "2026-07-05" and params["endDate"] == "2026-11-02"
    assert params["activityType"] == "fitness_equipment"
    fetched_sets = sorted(c[1] for c in garmin.calls if c[0] == "sets")
    assert fetched_sets == [23686126228, 24464818941, 24865183793, 24877545501]  # not the rowing
    schedule = [c[1] for c in garmin.calls if c[0] == "schedule"]
    assert schedule == ['query{workoutScheduleSummariesScalar(startDate:"2026-11-02", endDate:"2026-11-08")}']
    workouts = sorted(c[1] for c in garmin.calls if c[0] == "workout")
    assert workouts == [1716968605, 1729230803, 1743815609]  # strength only, not the run


def test_strength_route_caches_garmin_calls():
    now = [0.0]
    garmin = _FakeStrengthClient()
    client = _strength_app(garmin, now)
    client.get("/api/strength?date=2026-11-02", headers=AUTH)
    first = len(garmin.calls)

    client.get("/api/strength?date=2026-11-02", headers=AUTH)
    assert len(garmin.calls) == first  # report cache

    now[0] += 6 * 60  # report stale, everything else fresh
    client.get("/api/strength?date=2026-11-02", headers=AUTH)
    assert len(garmin.calls) == first

    now[0] += 20 * 60  # list, schedule and plans stale; settled sets are kept
    client.get("/api/strength?date=2026-11-02", headers=AUTH)
    kinds = [c[0] for c in garmin.calls[first:]]
    assert kinds.count("list") == 1
    assert kinds.count("schedule") == 1
    assert kinds.count("workout") == 3
    assert kinds.count("sets") == 0


def test_strength_route_rejects_bad_parameters():
    client = _strength_app(_FakeStrengthClient())
    for query in ("date=02-11-2026", "days=3", "days=400", "days=x", "log_parts=9"):
        assert client.get(f"/api/strength?{query}", headers=AUTH).status_code == 400, query


def test_strength_route_reports_garmin_failure_as_502():
    client = _strength_app(_FakeStrengthClient(fail_list=True))
    r = client.get("/api/strength?date=2026-11-02", headers=AUTH)
    assert r.status_code == 502
    assert "rate limited" in r.json()["error"]


def test_strength_route_skips_an_unreadable_activity():
    client = _strength_app(_FakeStrengthClient(fail_sets={24464818941}))
    body = client.get("/api/strength?date=2026-11-02", headers=AUTH).json()
    assert body["summary"]["activities_skipped"] == 1
    assert body["summary"]["sessions"] == 3


def test_strength_route_fails_when_no_activity_is_readable():
    ids = {a["activityId"] for a in STRENGTH_ACTIVITIES}
    client = _strength_app(_FakeStrengthClient(fail_sets=ids))
    assert client.get("/api/strength?date=2026-11-02", headers=AUTH).status_code == 502
