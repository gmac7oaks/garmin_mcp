"""Training dataset for the browser dashboards served at /training/.

One JSON document with everything the dashboards draw from: the full activity
history (trimmed to the fields the pages use) plus a year of daily wellness
data (sleep, HRV, resting heart rate, stress) and 90 days of Body Battery,
today's snapshot (summary, last night's sleep and HRV), heart-rate zones and
fitness age.

Garmin limits how much each request may cover, so ranged calls are chunked.
Each section is fetched independently: if one fails the rest of the document
is still served and the failure is listed under "errors".

The document is cached in memory and rebuilt in the background at each of
REFRESH_HOURS (UK time, following BST) and on demand.
"""

import datetime
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

ACTIVITY_PAGE = 100
WELLNESS_DAYS = 365
BODY_BATTERY_DAYS = 90
ENERGY_DAYS = 56  # calories burned per day
FOOD_DAYS = 28  # food logs, one request per day (older days are cached)
WEIGHT_DAYS = 120
CALENDAR_AHEAD_DAYS = 210
PREDICTION_DAYS = 120  # races on the Garmin calendar
REFRESH_HOURS = (7, 13, 20)  # UK local time; override with TRAINING_REFRESH_HOURS="7,13,20"


def refresh_hours_from_env() -> Tuple[int, ...]:
    raw = os.getenv("TRAINING_REFRESH_HOURS", "")
    hours = sorted({int(h) for h in raw.replace(" ", "").split(",") if h.isdigit() and 0 <= int(h) <= 23})
    return tuple(hours) or REFRESH_HOURS


def _last_sunday(year: int, month: int) -> datetime.date:
    day = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
    return day - datetime.timedelta(days=(day.weekday() + 1) % 7)


def uk_offset(utc: datetime.datetime) -> datetime.timedelta:
    """UK offset from UTC at a UTC instant: BST runs from 01:00 UTC on the last
    Sunday of March to 01:00 UTC on the last Sunday of October."""
    start = datetime.datetime.combine(_last_sunday(utc.year, 3), datetime.time(1))
    end = datetime.datetime.combine(_last_sunday(utc.year, 10), datetime.time(1))
    return datetime.timedelta(hours=1) if start <= utc.replace(tzinfo=None) < end else datetime.timedelta(0)

# Fields kept from each activity in the list endpoint.
_ACTIVITY_FIELDS = {
    "activityId": "id",
    "activityName": "name",
    "startTimeLocal": "start",
    "distance": "distance_m",
    "duration": "duration_s",
    "movingDuration": "moving_s",
    "averageHR": "avg_hr",
    "maxHR": "max_hr",
    "averageSpeed": "avg_speed",
    "elevationGain": "ascent_m",
    "calories": "calories",
    "activityTrainingLoad": "load",
    "aerobicTrainingEffect": "te_aerobic",
    "anaerobicTrainingEffect": "te_anaerobic",
    "trainingEffectLabel": "te_label",
    "vO2MaxValue": "vo2max",
    # Running dynamics (runs only; need a watch or HRM that records them).
    "averageRunningCadenceInStepsPerMinute": "cadence",
    "avgGroundContactTime": "gct_ms",
    "avgVerticalRatio": "vert_ratio",
    "avgVerticalOscillation": "vert_osc_cm",
    "avgStrideLength": "stride_cm",
    "avgPower": "power_w",
    "elevationLoss": "descent_m",
}


def _chunks(start: datetime.date, end: datetime.date, size: int) -> Iterable[Tuple[datetime.date, datetime.date]]:
    while start <= end:
        stop = min(end, start + datetime.timedelta(days=size - 1))
        yield start, stop
        start = stop + datetime.timedelta(days=1)


def _sport(type_key: str) -> str:
    key = (type_key or "").lower()
    if "run" in key:
        return "run"
    if "cycl" in key or "bik" in key or "ride" in key:
        return "bike"
    if "swim" in key:
        return "swim"
    if "strength" in key or "fitness_equipment" in key or "hiit" in key:
        return "lift"
    if "walk" in key or "hik" in key:
        return "walk"
    return "other"


def trim_activity(raw: Dict[str, Any]) -> Dict[str, Any]:
    out = {short: raw.get(long) for long, short in _ACTIVITY_FIELDS.items() if raw.get(long) is not None}
    type_key = (raw.get("activityType") or {}).get("typeKey", "")
    out["type"] = type_key
    out["sport"] = _sport(type_key)
    zones = [raw.get(f"hrTimeInZone_{i}") for i in range(1, 6)]
    if any(z is not None for z in zones):
        out["zones_s"] = [round(z or 0) for z in zones]
    return out


def _bb_day(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    values = [v[1] for v in entry.get("bodyBatteryValuesArray") or [] if len(v) > 1 and v[1] is not None]
    day = entry.get("date") or entry.get("calendarDate")
    if not day:
        return None
    return {
        "date": day,
        "high": max(values) if values else None,
        "low": min(values) if values else None,
        "latest": values[-1] if values else None,
        "charged": entry.get("charged"),
        "drained": entry.get("drained"),
    }


class TrainingData:
    """Builds and caches the dashboard dataset."""

    def __init__(
        self,
        client,
        config: Optional[Dict[str, Any]] = None,
        clock: Callable[[], float] = time.time,
        today: Callable[[], datetime.date] = datetime.date.today,
        max_age: float = 26 * 3600,
        refresh_hours: Optional[Iterable[int]] = None,
    ):
        self._client = client
        self._config = config or {}
        self._clock = clock
        self._today = today
        self._max_age = max_age
        self._refresh_hours = tuple(sorted(refresh_hours)) if refresh_hours else refresh_hours_from_env()
        self._doc: Optional[Dict[str, Any]] = None
        self._built_at = 0.0
        self._build_lock = threading.Lock()
        self._scheduler: Optional[threading.Thread] = None
        self._food_cache: Dict[str, Dict[str, Any]] = {}

    # ----- fetching -------------------------------------------------------

    def _api(self, path: str, params: Optional[Dict[str, Any]] = None):
        if params:
            return self._client.connectapi(path, params=params)
        return self._client.connectapi(path)

    def _display_name(self) -> str:
        name = getattr(self._client, "display_name", None)
        if not name:
            raise RuntimeError("Garmin display name unknown")
        return name

    def activities(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        start = 0
        while True:
            page = self._client.get_activities(start, ACTIVITY_PAGE) or []
            out.extend(trim_activity(a) for a in page)
            if len(page) < ACTIVITY_PAGE:
                return out
            start += ACTIVITY_PAGE

    def sleep(self, first: datetime.date, last: datetime.date) -> List[Dict[str, Any]]:
        out = []
        for a, b in _chunks(first, last, 28):
            data = self._api(f"/sleep-service/stats/sleep/daily/{a}/{b}") or {}
            days = data if isinstance(data, list) else data.get("individualStats") or []
            for day in days:
                v = day.get("values") or {}
                out.append(
                    {
                        "date": day.get("calendarDate"),
                        "duration_s": v.get("totalSleepTimeInSeconds"),
                        "score": v.get("sleepScore"),
                        "deep_s": v.get("deepTime"),
                        "rem_s": v.get("remTime"),
                        "light_s": v.get("lightTime"),
                        "awake_s": v.get("awakeTime"),
                    }
                )
        return out

    def hrv(self, first: datetime.date, last: datetime.date) -> List[Dict[str, Any]]:
        out = []
        for a, b in _chunks(first, last, 28):
            data = self._api(f"/hrv-service/hrv/daily/{a}/{b}") or {}
            for s in data if isinstance(data, list) else data.get("hrvSummaries") or []:
                base = s.get("baseline") or {}
                out.append(
                    {
                        "date": s.get("calendarDate"),
                        "last_night": s.get("lastNightAvg"),
                        "weekly": s.get("weeklyAvg"),
                        "status": s.get("status"),
                        "low": base.get("balancedLow"),
                        "high": base.get("balancedUpper"),
                    }
                )
        return out

    def resting_hr(self, first: datetime.date, last: datetime.date) -> List[Dict[str, Any]]:
        out = []
        url = f"/userstats-service/wellness/daily/{self._display_name()}"
        for a, b in _chunks(first, last, 90):
            data = self._api(url, {"fromDate": str(a), "untilDate": str(b), "metricId": 60}) or {}
            rows = ((data.get("allMetrics") or {}).get("metricsMap") or {}).get("WELLNESS_RESTING_HEART_RATE") or []
            out.extend({"date": r.get("calendarDate"), "rhr": r.get("value")} for r in rows if r.get("value"))
        return out

    def stress(self, first: datetime.date, last: datetime.date) -> List[Dict[str, Any]]:
        out = []
        for a, b in _chunks(first, last, 28):
            data = self._api(f"/usersummary-service/stats/stress/daily/{a}/{b}") or []
            for d in data if isinstance(data, list) else data.get("values") or []:
                v = d.get("values") or d
                out.append({"date": d.get("calendarDate"), "stress": v.get("overallStressLevel")})
        return out

    def body_battery(self, first: datetime.date, last: datetime.date) -> List[Dict[str, Any]]:
        out = []
        for a, b in _chunks(first, last, 31):
            for entry in self._client.get_body_battery(str(a), str(b)) or []:
                row = _bb_day(entry)
                if row:
                    out.append(row)
        return out

    def today_snapshot(self, day: datetime.date) -> Dict[str, Any]:
        snap: Dict[str, Any] = {}
        summary = self._client.get_user_summary(str(day)) or {}
        snap["summary"] = {
            "rhr": summary.get("restingHeartRate"),
            "rhr_7d": summary.get("lastSevenDaysAvgRestingHeartRate"),
            "stress": summary.get("averageStressLevel"),
            "body_battery": summary.get("bodyBatteryMostRecentValue"),
            "body_battery_high": summary.get("bodyBatteryHighestValue"),
            "steps": summary.get("totalSteps"),
        }
        sleep = (self._client.get_sleep_data(str(day)) or {}).get("dailySleepDTO") or {}
        scores = sleep.get("sleepScores") or {}
        snap["sleep"] = {
            "date": sleep.get("calendarDate"),
            "duration_s": sleep.get("sleepTimeSeconds"),
            "score": (scores.get("overall") or {}).get("value"),
            "deep_s": sleep.get("deepSleepSeconds"),
            "rem_s": sleep.get("remSleepSeconds"),
            "light_s": sleep.get("lightSleepSeconds"),
            "awake_s": sleep.get("awakeSleepSeconds"),
        }
        hrv = (self._client.get_hrv_data(str(day)) or {}).get("hrvSummary") or {}
        base = hrv.get("baseline") or {}
        snap["hrv"] = {
            "last_night": hrv.get("lastNightAvg"),
            "weekly": hrv.get("weeklyAvg"),
            "status": hrv.get("status"),
            "low": base.get("balancedLow"),
            "high": base.get("balancedUpper"),
        }
        return snap

    def zones(self) -> Any:
        return self._api("/biometric-service/heartRateZones")

    def fitness_age(self, day: datetime.date) -> Dict[str, Any]:
        data = self._client.get_fitnessage_data(str(day)) or {}
        return {
            "fitness_age": data.get("fitnessAge"),
            "chronological_age": data.get("chronologicalAge"),
            "achievable": data.get("achievableFitnessAge"),
        }

    @staticmethod
    def _prediction(data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "date": str(data.get("calendarDate") or "")[:10] or None,
            "5k": data.get("time5K"),
            "10k": data.get("time10K"),
            "half": data.get("timeHalfMarathon"),
            "marathon": data.get("timeMarathon"),
        }

    def race_predictions(self, day: datetime.date) -> Dict[str, Any]:
        out = self._prediction(self._client.get_race_predictions() or {})
        try:  # daily history for trend lines; optional
            first = day - datetime.timedelta(days=PREDICTION_DAYS - 1)
            rows = self._client.get_race_predictions(str(first), str(day), "daily") or []
            if isinstance(rows, dict):
                rows = rows.get("racePredictions") or rows.get("values") or [rows]
            out["history"] = [self._prediction(r) for r in rows if isinstance(r, dict) and r.get("time10K")]
        except Exception as exc:
            logger.info("race prediction history unavailable: %s", exc)
            out["history"] = []
        return out

    def calendar(self, day: datetime.date) -> Dict[str, Any]:
        """Races on the Garmin calendar and the next three weeks of scheduled workouts."""
        last = day + datetime.timedelta(days=CALENDAR_AHEAD_DAYS)
        year, month = day.year, day.month
        events, scheduled, seen = [], [], set()
        while (year, month) <= (last.year, last.month):
            data = self._client.get_scheduled_workouts(year, month) or {}
            for item in data.get("calendarItems") or []:
                if not isinstance(item, dict):
                    continue
                date = str(item.get("date") or "")
                key = (item.get("id"), date, item.get("itemType"))
                if not date or date < day.isoformat() or key in seen:
                    continue
                seen.add(key)
                if item.get("itemType") == "event":
                    target = item.get("completionTarget") or {}
                    unit = {"meter": 1.0, "kilometer": 1000.0, "mile": 1609.344, "yard": 0.9144}.get(target.get("unit"))
                    events.append({
                        "title": item.get("title"),
                        "date": date,
                        "race": bool(item.get("isRace")),
                        "primary": bool(item.get("primaryEvent")),
                        "distance_m": target.get("value") * unit if unit and isinstance(target.get("value"), (int, float)) else None,
                        "location": item.get("location"),
                    })
                elif item.get("itemType") == "workout" and date <= (day + datetime.timedelta(days=21)).isoformat():
                    scheduled.append({"date": date, "title": item.get("title"), "sport": (item.get("sportTypeKey") or "")})
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        events.sort(key=lambda e: e["date"])
        scheduled.sort(key=lambda w: w["date"])
        return {"events": events, "scheduled": scheduled}

    def energy(self, first: datetime.date, last: datetime.date) -> List[Dict[str, Any]]:
        out = []
        for a, b in _chunks(first, last, 28):
            data = self._api(f"/usersummary-service/stats/daily/{a}/{b}", {"statsType": "CALORIES"}) or {}
            for row in data.get("values") or []:
                v = row.get("values") or {}
                if v.get("totalCalories") is not None:
                    out.append({
                        "date": row.get("calendarDate"),
                        "total": v.get("totalCalories"),
                        "active": v.get("activeCalories"),
                        "resting": v.get("restingCalories"),
                    })
        return out

    def food(self, first: datetime.date, last: datetime.date) -> List[Dict[str, Any]]:
        """Daily food-log totals. Days more than two days old are cached."""
        out = []
        day = first
        settled = last - datetime.timedelta(days=2)
        while day <= last:
            key = day.isoformat()
            row = self._food_cache.get(key) if day < settled else None
            if row is None:
                data = self._api(f"/nutrition-service/food/logs/{key}") or {}
                content = data.get("dailyNutritionContent") or {}
                goals = data.get("dailyNutritionGoals") or {}
                row = {
                    "date": key,
                    "calories": content.get("calories"),
                    "protein": content.get("protein"),
                    "carbs": content.get("carbs"),
                    "fat": content.get("fat"),
                    "fibre": content.get("fiber"),
                    "goal_calories": goals.get("calories"),
                    "goal_protein": goals.get("protein"),
                    "goal_carbs": goals.get("carbs"),
                    "goal_fat": goals.get("fat"),
                }
                if day < settled:
                    self._food_cache[key] = row
            out.append(row)
            day += datetime.timedelta(days=1)
        return out

    def weight(self, first: datetime.date, last: datetime.date) -> List[Dict[str, Any]]:
        data = self._client.get_body_composition(str(first), str(last)) or {}
        out = []
        for row in data.get("dateWeightList") or []:
            grams = row.get("weight")
            if grams:
                out.append({
                    "date": row.get("calendarDate"),
                    "kg": round(grams / 1000, 2),
                    "fat_pct": row.get("bodyFat"),
                })
        out.sort(key=lambda r: r["date"] or "")
        return out

    # ----- building -------------------------------------------------------

    def build(self) -> Dict[str, Any]:
        day = self._today()
        year_ago = day - datetime.timedelta(days=WELLNESS_DAYS - 1)
        bb_from = day - datetime.timedelta(days=BODY_BATTERY_DAYS - 1)
        sections: List[Tuple[str, Callable[[], Any]]] = [
            ("activities", self.activities),
            ("sleep", lambda: self.sleep(year_ago, day)),
            ("hrv", lambda: self.hrv(year_ago, day)),
            ("resting_hr", lambda: self.resting_hr(year_ago, day)),
            ("stress", lambda: self.stress(year_ago, day)),
            ("body_battery", lambda: self.body_battery(bb_from, day)),
            ("today", lambda: self.today_snapshot(day)),
            ("hr_zones", self.zones),
            ("fitness_age", lambda: self.fitness_age(day)),
            ("race_predictions", lambda: self.race_predictions(day)),
            ("calendar", lambda: self.calendar(day)),
            ("energy", lambda: self.energy(day - datetime.timedelta(days=ENERGY_DAYS - 1), day)),
            ("food", lambda: self.food(day - datetime.timedelta(days=FOOD_DAYS - 1), day)),
            ("weight", lambda: self.weight(day - datetime.timedelta(days=WEIGHT_DAYS - 1), day)),
        ]
        doc: Dict[str, Any] = {"errors": {}}
        for name, fetch in sections:
            try:
                doc[name] = fetch()
            except Exception as exc:  # one failing section must not sink the rest
                logger.warning("training data: %s failed: %s", name, exc)
                doc[name] = None
                doc["errors"][name] = str(exc)[:300]
        if doc.get("activities") is None:
            raise RuntimeError(f"activities failed: {doc['errors'].get('activities')}")
        doc["config"] = dict(self._config)
        doc["date"] = day.isoformat()
        doc["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        return doc

    def get(self, refresh: bool = False) -> Dict[str, Any]:
        fresh = self._doc is not None and self._clock() - self._built_at < self._max_age
        if fresh and not refresh:
            return self._doc
        with self._build_lock:
            fresh = self._doc is not None and self._clock() - self._built_at < self._max_age
            if refresh or not fresh:
                started = self._clock()
                self._doc = self.build()
                self._built_at = started
        return self._doc

    # ----- daily refresh --------------------------------------------------

    def seconds_until_refresh(self, now_utc: Optional[datetime.datetime] = None) -> float:
        """Seconds from now_utc (naive UTC) to the next refresh hour in UK time."""
        now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        local_day = (now_utc + uk_offset(now_utc)).date()
        for offset_days in range(3):
            day = local_day + datetime.timedelta(days=offset_days)
            for hour in self._refresh_hours:
                local = datetime.datetime.combine(day, datetime.time(hour))
                target = local - uk_offset(local - datetime.timedelta(hours=1))
                if target > now_utc:
                    return (target - now_utc).total_seconds()
        return 24 * 3600.0

    def start_daily_refresh(self) -> None:
        if self._scheduler is not None:
            return

        def loop():
            try:  # warm the cache so the first page view is quick
                self.get()
            except Exception as exc:
                logger.warning("initial training data build failed: %s", exc)
            while True:
                time.sleep(self.seconds_until_refresh())
                try:
                    self.get(refresh=True)
                    logger.info("training data refreshed")
                except Exception as exc:
                    logger.warning("scheduled training refresh failed: %s", exc)

        self._scheduler = threading.Thread(target=loop, name="training-refresh", daemon=True)
        self._scheduler.start()
