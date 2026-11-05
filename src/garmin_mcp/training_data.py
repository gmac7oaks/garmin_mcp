"""Training dataset for the browser dashboards served at /training/.

One JSON document with everything the dashboards draw from: the full activity
history (trimmed to the fields the pages use) plus a year of daily wellness
data (sleep, HRV, resting heart rate, stress) and 90 days of Body Battery,
today's snapshot (summary, last night's sleep and HRV), heart-rate zones and
fitness age.

Garmin limits how much each request may cover, so ranged calls are chunked.
Each section is fetched independently: if one fails the rest of the document
is still served and the failure is listed under "errors".

The document is cached in memory, rebuilt once a day in the background
(REFRESH_HOUR local time) and on demand.
"""

import datetime
import logging
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

ACTIVITY_PAGE = 100
WELLNESS_DAYS = 365
BODY_BATTERY_DAYS = 90
REFRESH_HOUR = 4  # daily rebuild, server local time

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
    ):
        self._client = client
        self._config = config or {}
        self._clock = clock
        self._today = today
        self._max_age = max_age
        self._doc: Optional[Dict[str, Any]] = None
        self._built_at = 0.0
        self._build_lock = threading.Lock()
        self._scheduler: Optional[threading.Thread] = None

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

    def seconds_until_refresh(self, now: Optional[datetime.datetime] = None) -> float:
        now = now or datetime.datetime.now()
        target = now.replace(hour=REFRESH_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        return (target - now).total_seconds()

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
                    logger.warning("daily training refresh failed: %s", exc)

        self._scheduler = threading.Thread(target=loop, name="training-refresh", daemon=True)
        self._scheduler.start()
