"""Tests for the training dashboard data builder and its HTTP routes."""
import datetime

from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from garmin_mcp import training_data as td
from garmin_mcp import training_routes as tr

TOKEN = "dash-token"
DAY = datetime.date(2026, 11, 5)


class FakeGarmin:
    display_name = "guy"

    def __init__(self, fail=()):
        self.fail = set(fail)
        self.calls = []

    def _check(self, name):
        self.calls.append(name)
        if name in self.fail:
            raise RuntimeError(f"{name} broke")

    def get_activities(self, start, limit):
        self._check("activities")
        if start:
            return []
        return [
            {
                "activityId": 1,
                "activityName": "Easy run",
                "startTimeLocal": "2026-11-04 16:38:00",
                "activityType": {"typeKey": "running"},
                "distance": 7260.0,
                "duration": 2265.0,
                "averageHR": 108,
                "activityTrainingLoad": 40.5,
                "hrTimeInZone_1": 2200,
                "hrTimeInZone_2": 65,
            },
            {
                "activityId": 2,
                "activityName": "Gym",
                "startTimeLocal": "2026-11-03 07:00:00",
                "activityType": {"typeKey": "strength_training"},
                "duration": 3600.0,
            },
        ]

    def connectapi(self, path, params=None):
        if "sleep" in path:
            self._check("sleep")
            return [{"calendarDate": "2026-11-05", "values": {"totalSleepTimeInSeconds": 26000, "sleepScore": 78}}]
        if "hrv" in path:
            self._check("hrv")
            return {"hrvSummaries": [{"calendarDate": "2026-11-05", "lastNightAvg": 48,
                                      "baseline": {"balancedLow": 42, "balancedUpper": 55}}]}
        if "wellness" in path:
            self._check("resting_hr")
            return {"allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [
                {"calendarDate": "2026-11-05", "value": 51}]}}}
        if "stats/daily" in path:
            self._check("energy")
            return {"values": [{"calendarDate": "2026-11-05", "values": {"totalCalories": 2900, "activeCalories": 900, "restingCalories": 2000}}]}
        if "food/logs" in path:
            self._check("food")
            return {"dailyNutritionContent": {"calories": 2400, "protein": 170, "carbs": 260, "fat": 80, "fiber": 30},
                    "dailyNutritionGoals": {"calories": 2600, "protein": 170}}
        if "stress" in path:
            self._check("stress")
            return [{"calendarDate": "2026-11-05", "values": {"overallStressLevel": 28}}]
        if "heartRateZones" in path:
            self._check("hr_zones")
            return []
        raise AssertionError(path)

    def get_body_battery(self, a, b):
        self._check("body_battery")
        return []

    def get_user_summary(self, day):
        self._check("today")
        return {"restingHeartRate": 51, "bodyBatteryMostRecentValue": 62}

    def get_sleep_data(self, day):
        return {}

    def get_hrv_data(self, day):
        return {}

    def get_race_predictions(self, *args):
        self._check("race_predictions")
        if args:
            return [{"calendarDate": "2026-11-04", "time5K": 1150, "time10K": 2360},
                    {"calendarDate": "2026-11-05", "time5K": 1143, "time10K": 2349}]
        return {"calendarDate": "2026-11-05", "time5K": 1143, "time10K": 2349,
                "timeHalfMarathon": 5258, "timeMarathon": 11400}

    def get_scheduled_workouts(self, year, month):
        self._check("calendar")
        if (year, month) != (2026, 11):
            return {"calendarItems": []}
        return {"calendarItems": [
            {"id": 1, "itemType": "event", "date": "2026-11-15", "title": "Marden 10km", "isRace": True,
             "primaryEvent": True, "completionTarget": {"value": 10, "unit": "kilometer"}},
            {"id": 2, "itemType": "workout", "date": "2026-11-07", "title": "Easy 40", "sportTypeKey": "running"},
            {"id": 3, "itemType": "workout", "date": "2026-11-01", "title": "Past run"},
            {"id": 4, "itemType": "weight", "date": "2026-11-06"},
        ]}

    def get_body_composition(self, a, b):
        self._check("weight")
        return {"dateWeightList": [{"calendarDate": "2026-11-05", "weight": 84130.0, "bodyFat": 25.7}]}

    def get_fitnessage_data(self, day):
        self._check("fitness_age")
        return {"fitnessAge": 38.2, "chronologicalAge": 45}


def make(client, **kw):
    return td.TrainingData(client, config={"goal_race": "Marden 10K"}, today=lambda: DAY, **kw)


def test_trim_activity_maps_fields_and_sport():
    raw = FakeGarmin().get_activities(0, 100)[0]
    a = td.trim_activity(raw)
    assert a["id"] == 1 and a["sport"] == "run"
    assert a["distance_m"] == 7260.0 and a["avg_hr"] == 108
    assert a["start"].startswith("2026-11-04")


def test_build_collects_sections_and_config():
    doc = make(FakeGarmin()).build()
    assert len(doc["activities"]) == 2
    assert doc["config"]["goal_race"] == "Marden 10K"
    assert doc["date"] == "2026-11-05"
    assert doc["today"]["summary"]["rhr"] == 51
    assert doc["fitness_age"]["fitness_age"] == 38.2


def test_build_isolates_failing_sections():
    doc = make(FakeGarmin(fail={"stress", "hrv"})).build()
    assert doc["stress"] is None and doc["hrv"] is None
    assert set(doc["errors"]) == {"stress", "hrv"}
    assert doc["activities"]


def test_build_raises_when_activities_fail():
    try:
        make(FakeGarmin(fail={"activities"})).build()
    except RuntimeError as exc:
        assert "activities" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_get_caches_until_stale_or_refresh():
    now = [1000.0]
    client = FakeGarmin()
    data = make(client, clock=lambda: now[0], max_age=100)
    data.get()
    n = client.calls.count("activities")
    data.get()
    assert client.calls.count("activities") == n
    data.get(refresh=True)
    assert client.calls.count("activities") > n
    n = client.calls.count("activities")
    now[0] += 101
    data.get()
    assert client.calls.count("activities") > n


def test_refresh_runs_at_7_13_20_uk_time():
    data = make(FakeGarmin(), refresh_hours=(7, 13, 20))
    nxt = lambda *a: data.seconds_until_refresh(datetime.datetime(*a)) / 3600  # noqa: E731
    # Winter (GMT = UTC)
    assert nxt(2026, 11, 5, 6, 0) == 1
    assert nxt(2026, 11, 5, 7, 0) == 6
    assert nxt(2026, 11, 5, 19, 30) == 0.5
    assert nxt(2026, 11, 5, 21, 0) == 10  # next day 07:00
    # Summer (BST = UTC+1): 07:00 BST is 06:00 UTC
    assert nxt(2026, 7, 1, 5, 0) == 1
    assert nxt(2026, 7, 1, 18, 0) == 1  # 20:00 BST = 19:00 UTC
    assert nxt(2026, 7, 1, 19, 0) == 11  # then 07:00 BST next day = 06:00 UTC
    # Clocks go forward on Sun 29 Mar 2026: Sat 20:00 GMT -> Sun 07:00 BST (06:00 UTC)
    assert nxt(2026, 3, 28, 20, 0) == 10


def test_refresh_hours_from_env(monkeypatch):
    monkeypatch.setenv("TRAINING_REFRESH_HOURS", "20, 7,13,99,x")
    assert td.refresh_hours_from_env() == (7, 13, 20)
    monkeypatch.delenv("TRAINING_REFRESH_HOURS")
    assert td.refresh_hours_from_env() == (7, 13, 20)


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("TRAINING_GOAL_RACE", "Marden 10K")
    monkeypatch.setenv("TRAINING_GOAL_DATE", "2026-11-15")
    monkeypatch.setenv("TRAINING_BLOCK_LABELS", "2026-11-09=TAPER; bad ;2026-11-16=RACE")
    cfg = tr.config_from_env()
    assert cfg["goal_date"] == "2026-11-15"
    assert cfg["block_labels"] == {"2026-11-09": "TAPER", "2026-11-16": "RACE"}


class StubData:
    def __init__(self, fail=False):
        self.fail = fail
        self.refreshes = []

    def get(self, refresh=False):
        self.refreshes.append(refresh)
        if self.fail:
            raise RuntimeError("garmin down")
        return {"activities": [], "errors": {}}

    def start_daily_refresh(self):
        pass


def client(data):
    fastmcp = FastMCP("test")
    tr.register_training_routes(fastmcp, FakeGarmin(), TOKEN, data=data)
    return TestClient(fastmcp.streamable_http_app())


def test_routes_require_token():
    c = client(StubData())
    for path in ("/training/data.json", "/training/", "/training/coach"):
        assert c.get(path).status_code == 401
        assert c.get(path + "?key=wrong").status_code == 401


def test_data_json_with_key_and_bearer():
    data = StubData()
    c = client(data)
    r = c.get(f"/training/data.json?key={TOKEN}")
    assert r.status_code == 200 and r.json()["activities"] == []
    assert r.headers["cache-control"] == "no-store"
    assert c.get("/training/data.json?refresh=1", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert data.refreshes == [False, True]


def test_data_json_failure_is_502():
    r = client(StubData(fail=True)).get(f"/training/data.json?key={TOKEN}")
    assert r.status_code == 502 and "garmin down" in r.json()["error"]


def test_every_page_is_served():
    c = client(StubData())
    for name in tr.PAGES:
        r = c.get(f"/training/{name}?key={TOKEN}")
        assert r.status_code == 200, name
        assert "common.js" in r.text
    assert c.get(f"/training/nope?key={TOKEN}").status_code == 404
    assert c.get(f"/training/..%2Fsecret?key={TOKEN}").status_code == 404
    index = c.get(f"/training/?key={TOKEN}").text
    assert all(f"/training/{n}?" in index for n in tr.PAGES)


def test_common_js_is_public():
    r = client(StubData()).get("/training/common.js")
    assert r.status_code == 200 and "window.T" in r.text or "T.prepare" in r.text


def test_new_sections():
    client = FakeGarmin()
    data = make(client)
    doc = data.build()
    assert doc["errors"] == {}
    rp = doc["race_predictions"]
    assert rp["10k"] == 2349 and len(rp["history"]) == 2
    cal = doc["calendar"]
    assert cal["events"] == [{"title": "Marden 10km", "date": "2026-11-15", "race": True, "primary": True,
                              "distance_m": 10000.0, "location": None}]
    assert [w["title"] for w in cal["scheduled"]] == ["Easy 40"]
    assert doc["energy"][0]["total"] == 2900
    assert len(doc["food"]) == td.FOOD_DAYS and doc["food"][-1]["protein"] == 170
    assert doc["weight"] == [{"date": "2026-11-05", "kg": 84.13, "fat_pct": 25.7}]
    # Settled food days are cached: a rebuild only re-reads the last three days.
    before = client.calls.count("food")
    data.build()
    assert client.calls.count("food") - before == 3


def test_trim_activity_keeps_running_dynamics():
    a = td.trim_activity({"activityId": 9, "averageRunningCadenceInStepsPerMinute": 166.4,
                          "avgGroundContactTime": 251.0, "avgVerticalRatio": 8.1, "avgStrideLength": 118.0,
                          "activityType": {"typeKey": "running"}})
    assert a["cadence"] == 166.4 and a["gct_ms"] == 251.0 and a["vert_ratio"] == 8.1 and a["stride_cm"] == 118.0


def _set(category, name, reps, grams, kind="ACTIVE"):
    return {"setType": kind, "repetitionCount": reps, "weight": grams,
            "exercises": [{"category": category, "name": name}] if category else []}


def test_strength_group_from_name():
    names = ["Push v3", "Tuesday's Legs", "Friday's Pull", "Legs - Race week (easy)", "Core v2", "Pushkin run", "Gym"]
    assert [td.strength_group(n) for n in names] == ["push", "legs", "pull", "legs", None, "push", None]


def test_summarise_sets_takes_top_weight_and_skips_noise():
    rows = td.summarise_sets([
        _set("ROW", "INDOOR_ROW", 110, 0),
        _set(None, None, None, None, kind="REST"),
        _set("BENCH_PRESS", "INCLINE_DUMBBELL_BENCH_PRESS", 12, 36000),
        _set("BENCH_PRESS", "INCLINE_DUMBBELL_BENCH_PRESS", 6, 68000),
        _set("BENCH_PRESS", "INCLINE_DUMBBELL_BENCH_PRESS", 6, 68000),
        _set("ROW", "BENT_OVER_ROW_WITH_DUMBELL", 10, 38000),  # a real row, unlike the rowing machine
        _set("WARM_UP", None, 5, 0),
        _set("PUSH_UP", "PUSH_UP", 26, 0),
        _set("PUSH_UP", "PUSH_UP", 16, None),
    ])
    assert [r["label"] for r in rows] == ["Incline dumbbell bench press", "Bent over row with dumbbell", "Push up"]
    incline, row, push_up = rows
    assert row["top_kg"] == 38.0
    assert incline["top_kg"] == 68.0 and incline["reps_at_top"] == [6, 6]
    assert incline["sets"] == 3 and incline["volume_kg"] == 12 * 36 + 2 * 6 * 68
    assert push_up["top_kg"] == 0 and push_up["max_reps"] == 26


class LiftingGarmin(FakeGarmin):
    def __init__(self):
        super().__init__()
        self.set_calls = []

    def get_activities(self, start, limit):
        if start:
            return []
        lift = {"activityType": {"typeKey": "strength_training"}}
        return [
            {**lift, "activityId": 10, "activityName": "Push v3", "startTimeLocal": "2026-11-04 05:38:00"},
            {**lift, "activityId": 11, "activityName": "Legs - Race week (easy)", "startTimeLocal": "2026-11-03 05:40:00"},
            {**lift, "activityId": 12, "activityName": "Push v2", "startTimeLocal": "2026-10-27 05:37:00"},
            {**lift, "activityId": 13, "activityName": "Core v2", "startTimeLocal": "2026-10-25 08:00:00"},
            {**lift, "activityId": 14, "activityName": "Push", "startTimeLocal": "2026-01-01 05:00:00"},  # too old
            {"activityId": 15, "activityName": "Push the pace", "startTimeLocal": "2026-11-02 07:00:00",
             "activityType": {"typeKey": "running"}},
        ]

    def get_activity_exercise_sets(self, activity_id):
        self.set_calls.append(activity_id)
        kg = {10: 75000, 11: 45000, 12: 70000}[activity_id]
        return {"exerciseSets": [_set("BENCH_PRESS", None, 8, kg), _set("BENCH_PRESS", None, 7, kg)]}


def test_strength_sessions_and_caching():
    client = LiftingGarmin()
    data = make(client)
    rows = data.build()["strength"]
    assert [(r["id"], r["group"], r["light"]) for r in rows] == [(12, "push", False), (11, "legs", True), (10, "push", False)]
    assert rows[-1]["exercises"][0]["top_kg"] == 75.0 and rows[-1]["exercises"][0]["reps_at_top"] == [8, 7]
    # Sessions older than three days are cached; recent ones are re-read on the next build.
    data.build()
    assert sorted(client.set_calls) == [10, 10, 11, 11, 12]


def test_strength_page_is_listed():
    assert tr.PAGES["strength"] == "Strength Progression"


SHARE = "coach-view-only"


class FullDoc(StubData):
    def get(self, refresh=False):
        self.refreshes.append(refresh)
        return {"activities": [{"id": 1}], "sleep": [{"date": "2026-11-05"}], "weight": [{"kg": 84}],
                "strength": [{"date": "2026-11-04", "group": "push"}], "date": "2026-11-05",
                "updated": "2026-11-05T07:00:00+00:00", "errors": {"food": "x", "strength": "y"}}


def share_client(data):
    fastmcp = FastMCP("test")
    tr.register_training_routes(fastmcp, FakeGarmin(), TOKEN, data=data, share_token=SHARE)
    return TestClient(fastmcp.streamable_http_app())


def test_share_key_opens_only_the_strength_page():
    c = share_client(FullDoc())
    assert c.get(f"/training/strength?key={SHARE}").status_code == 200
    for path in ("/training/coach", "/training/fuel", "/training/", "/training/nope"):
        assert c.get(f"{path}?key={SHARE}").status_code == 401, path
    # The owner key still opens everything.
    assert c.get(f"/training/fuel?key={TOKEN}").status_code == 200


def test_share_key_data_is_strength_only_and_cannot_refresh():
    data = FullDoc()
    c = share_client(data)
    doc = c.get(f"/training/data.json?key={SHARE}&refresh=1").json()
    assert doc["strength"] == [{"date": "2026-11-04", "group": "push"}]
    assert doc["activities"] == [] and "sleep" not in doc and "weight" not in doc
    assert doc["errors"] == {"strength": "y"} and doc["shared"] is True
    full = c.get(f"/training/data.json?key={TOKEN}").json()
    assert full["sleep"] and full["weight"]
    assert data.refreshes == [False, False]


def test_share_token_must_differ_and_is_optional():
    import pytest
    with pytest.raises(ValueError):
        tr.register_training_routes(FastMCP("t"), FakeGarmin(), TOKEN, data=StubData(), share_token=TOKEN)
    c = client(StubData())  # no share token configured
    assert c.get("/training/strength?key=").status_code == 401


class NamedGarmin(FakeGarmin):
    def get_full_name(self):
        return "James Example"


def test_athlete_name_comes_from_garmin_unless_configured():
    assert make(NamedGarmin()).build()["config"]["athlete"] == "James Example"
    data = td.TrainingData(NamedGarmin(), config={"athlete": "Set Name"}, today=lambda: DAY)
    assert data.build()["config"]["athlete"] == "Set Name"
    assert make(FakeGarmin()).build()["config"]["athlete"] is None  # no name method: page shows "Athlete"


def test_coach_name_defaults_to_claude(monkeypatch):
    monkeypatch.delenv("TRAINING_COACH", raising=False)
    assert tr.config_from_env()["coach"] == "Claude"
    monkeypatch.setenv("TRAINING_COACH", "Aaron")
    assert tr.config_from_env()["coach"] == "Aaron"
    assert "Guy McLaughlin" not in (tr.PAGES_DIR / "coach.html").read_text()
