"""Tests for the plain HTTP API (GET /api/nutrition)."""
import datetime

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from garmin_mcp import http_api

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
