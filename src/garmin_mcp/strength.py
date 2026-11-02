"""Strength-training summaries for dashboards.

Turns Garmin strength activities (with their exercise sets) and the week's
scheduled strength workouts into compact JSON:

* a session log (each workout and its exercises, e.g. "60 kg x 8·8·8");
* per-exercise load progression (top weight by week, change over 4 weeks and
  since the start of the range, weeks at the current weight);
* for every exercise in this week's scheduled workouts, what to do with the
  weight next time.

The suggestion uses double progression: once every planned set reaches the
rep (or time) target at the planned weight, the weight goes up by the
smallest sensible step; until then it stays put. Reps are whatever the watch
logged, so a set that was edited or auto-counted wrongly skews the advice.

Everything here is pure data shaping; fetching and caching live in http_api.
"""

from __future__ import annotations

import datetime
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SPARK_LEVELS = "▁▂▃▄▅▆▇█"
SPARK_GAP = "·"
LB_TO_KG = 0.45359237

# Sets in these categories are warm-up cardio or unrecognised, not lifting.
_SKIP_CATEGORIES = frozenset(
    {
        "UNKNOWN",
        "WARM_UP",
        "CARDIO",
        "RUN",
        "BIKE",
        "INDOOR_BIKE",
        "INDOOR_ROW",
        "ELLIPTICAL",
        "STAIR_STEPPER",
        "FLOOR_CLIMB",
    }
)
_SKIP_EXERCISES = frozenset({"INDOOR_ROW", "WALK", "RUN", "JOG"})
_GROUPS = (("push", "Push"), ("pull", "Pull"), ("leg", "Legs"), ("core", "Core"))
GROUP_ORDER = ("Push", "Pull", "Legs", "Core", "Other")

_REP_RANGE = re.compile(r"(\d+)\s*[-–]\s*(\d+)")
_PAIR = re.compile(r"\b2\s*[x×]\s*\d")
# Words that mark a description chunk as an instruction rather than a name.
_NOT_A_NAME = frozenset(
    {"failure", "reps", "rep", "each", "per", "side", "intent", "single", "set", "lap", "max", "seated"}
)

# Statuses, in the order the dashboard should care about them.
STATUS_LABELS = {
    "increase": "Go up",
    "below": "Plan below last",
    "stalled": "Same weight 3+ wk",
    "hold": "Building reps",
    "up": "Increased",
    "down": "Dropped",
    "failure": "To failure",
    "bodyweight": "Bodyweight",
    "new": "New",
}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _num(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _same(a: Optional[float], b: Optional[float]) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) < 0.1


def fmt_kg(weight: Optional[float]) -> str:
    """72.5 -> "72.5", 60.0 -> "60", 13.607 -> "13.6"."""
    if weight is None:
        return "–"
    text = f"{weight:.1f}"
    return text[:-2] if text.endswith(".0") else text


def monday(day: datetime.date) -> datetime.date:
    return day - datetime.timedelta(days=day.weekday())


def day_label(day: datetime.date) -> str:
    return f"{day:%a} {day.day} {day:%b}"


def workout_group(name: Optional[str]) -> str:
    """Push / Pull / Legs / Core from a workout or activity name."""
    lowered = (name or "").lower()
    for word, group in _GROUPS:
        if word in lowered:
            return group
    return "Other"


def exercise_key(category: Optional[str], name: Optional[str]) -> str:
    return f"{category or 'UNKNOWN'}/{name or ''}"


def _pretty(token: str) -> str:
    words = token.replace("DUMBELL", "DUMBBELL").lower().replace("_", " ")
    return words[:1].upper() + words[1:]


def exercise_label(category: Optional[str], name: Optional[str]) -> str:
    """Garmin's exercise name, or its category when no exercise was picked."""
    if name:
        return _pretty(name)
    return _pretty(category or "Unknown")


def label_from_description(description: Optional[str]) -> Optional[str]:
    """A name the user typed at the start of a step description, if any.

    "Flat bench, 6-8 reps" -> "Flat bench"; "Seated, 8-10 reps" -> None.
    """
    if not description:
        return None
    chunk = re.split(r"[,.;(]| - ", description, maxsplit=1)[0].strip()
    chunk = re.sub(r"\s*\d+\s*[-–]\s*\d+.*$", "", chunk)  # "Tibialis raises 15-20"
    words = chunk.split()
    if len(words) < 2 or any(ch.isdigit() for ch in chunk):
        return None
    if any(word.lower().strip("-") in _NOT_A_NAME for word in words):
        return None
    return chunk[:1].upper() + chunk[1:]


def _skip(category: Optional[str], name: Optional[str]) -> bool:
    return (category or "UNKNOWN") in _SKIP_CATEGORIES or (name or "") in _SKIP_EXERCISES


# --------------------------------------------------------------------------
# Performed sessions
# --------------------------------------------------------------------------


def parse_sets(data: Optional[dict]) -> List[dict]:
    """Active lifting sets from a /activity/{id}/exerciseSets response."""
    sets = []
    for raw in (data or {}).get("exerciseSets") or []:
        if raw.get("setType") != "ACTIVE":
            continue
        candidates = sorted(
            raw.get("exercises") or [],
            key=lambda e: -(_num(e.get("probability")) or 0.0),
        )
        if not candidates:
            continue
        category = candidates[0].get("category")
        name = candidates[0].get("name")
        if _skip(category, name):
            continue
        reps = _num(raw.get("repetitionCount"))
        grams = _num(raw.get("weight"))
        seconds = _num(raw.get("duration"))
        reps = int(reps) if reps else None
        if reps is None and not seconds:
            continue
        sets.append(
            {
                "key": exercise_key(category, name),
                "category": category,
                "name": name,
                "weight": round(grams / 1000.0, 2) if grams is not None else None,
                "reps": reps,
                "seconds": int(round(seconds)) if seconds else None,
            }
        )
    return sets


def _set_amount(item: dict, timed: bool) -> Optional[int]:
    return item.get("seconds") if timed else item.get("reps")


def format_sets(sets: Sequence[dict]) -> str:
    """Compact text for one exercise's sets, e.g. "16 kg × 8·8, 20 kg × 8"."""
    timed = all(not s.get("reps") for s in sets)
    # A set with no weight recorded is bodyweight when no set carried a load.
    bodyweight = not any(s.get("weight") for s in sets)
    parts: List[Tuple[Optional[float], List[str]]] = []
    for item in sets:
        amount = _set_amount(item, timed)
        text = "?" if amount is None else (f"{amount}s" if timed else str(amount))
        weight = item.get("weight")
        if weight is None and bodyweight:
            weight = 0.0
        if parts and _same(parts[-1][0], weight):
            parts[-1][1].append(text)
        else:
            parts.append((weight, [text]))
    out = []
    for weight, amounts in parts:
        if weight is None:
            prefix = "? kg × "
        elif weight == 0:
            prefix = "BW × "
        else:
            prefix = f"{fmt_kg(weight)} kg × "
        out.append(prefix + "·".join(amounts))
    return ", ".join(out)


def summarise_exercise(sets: Sequence[dict]) -> dict:
    """Top weight, the reps (or seconds) done at it, volume and e1RM."""
    weights = [s["weight"] for s in sets if s.get("weight") is not None]
    top = max(weights) if weights else None
    timed = all(not s.get("reps") for s in sets)
    at_top = [s for s in sets if _same(s.get("weight"), top)] if top is not None else list(sets)
    volume = sum((s.get("weight") or 0.0) * (s.get("reps") or 0) for s in sets)
    e1rm = max(
        (
            s["weight"] * (1 + min(s["reps"], 15) / 30.0)
            for s in sets
            if s.get("weight") and s.get("reps")
        ),
        default=None,
    )
    return {
        "top": top,
        "timed": timed,
        "done": [a for a in (_set_amount(s, timed) for s in at_top) if a],
        "pairs": [(s.get("weight"), _set_amount(s, timed)) for s in sets],
        "top_text": format_sets(at_top),
        "sets": len(sets),
        "volume": round(volume),
        "e1rm": round(e1rm, 1) if e1rm else None,
        "text": format_sets(sets),
    }


def summarise_session(activity: dict, raw_sets: Optional[dict]) -> dict:
    """One strength activity with its exercises in the order they were done."""
    start = str(activity.get("startTimeLocal") or activity.get("startTimeGMT") or "")
    name = activity.get("activityName") or "Strength"
    grouped: Dict[str, List[dict]] = {}
    for item in parse_sets(raw_sets):
        grouped.setdefault(item["key"], []).append(item)
    exercises = []
    for key, sets in grouped.items():
        exercises.append(
            {
                "key": key,
                "category": sets[0]["category"],
                "label": exercise_label(sets[0]["category"], sets[0]["name"]),
                **summarise_exercise(sets),
            }
        )
    duration = _num(activity.get("duration")) or 0.0
    return {
        "id": activity.get("activityId"),
        "date": start[:10],
        "name": name,
        "group": workout_group(name),
        "minutes": int(round(duration / 60)),
        "sets": sum(e["sets"] for e in exercises),
        "volume": sum(e["volume"] for e in exercises),
        "exercises": exercises,
    }


# --------------------------------------------------------------------------
# Planned workouts
# --------------------------------------------------------------------------


def _step_weight_kg(step: dict) -> Optional[float]:
    value = _num(step.get("weightValue"))
    if value is None:
        return None
    unit = ((step.get("weightUnit") or {}).get("unitKey") or "kilogram").lower()
    if unit.startswith("pound"):
        return round(value * LB_TO_KG, 1)
    return value


def _walk_steps(steps: Iterable[dict], sets: int = 1) -> Iterable[Tuple[dict, int]]:
    for step in steps or []:
        if step.get("type") == "RepeatGroupDTO" or step.get("workoutSteps"):
            iterations = int(_num(step.get("numberOfIterations")) or _num(step.get("endConditionValue")) or 1)
            yield from _walk_steps(step.get("workoutSteps") or [], sets * max(iterations, 1))
        else:
            yield step, sets


def plan_items(workout: dict) -> List[dict]:
    """Working exercises in a strength workout, merged by exercise."""
    items: Dict[str, dict] = {}
    for segment in workout.get("workoutSegments") or []:
        for step, sets in _walk_steps(segment.get("workoutSteps") or []):
            step_type = ((step.get("stepType") or {}).get("stepTypeKey") or "").lower()
            if step_type in ("warmup", "cooldown", "rest", "recovery", "recover", "repeat"):
                continue
            category = step.get("category")
            name = step.get("exerciseName")
            if not category or _skip(category, name):
                continue
            condition = ((step.get("endCondition") or {}).get("conditionTypeKey") or "").lower()
            value = _num(step.get("endConditionValue"))
            description = step.get("description") or ""
            key = exercise_key(category, name)
            item = items.get(key)
            if item is None:
                rep_range = _REP_RANGE.search(description)
                item = items[key] = {
                    "key": key,
                    "category": category,
                    "name": name,
                    "label": label_from_description(description) or exercise_label(category, name),
                    "description": description,
                    "weight": _step_weight_kg(step),
                    "sets": 0,
                    "reps": int(value) if condition == "reps" and value else None,
                    "seconds": int(value) if condition == "time" and value else None,
                    "rep_min": int(rep_range.group(1)) if rep_range else None,
                    "to_failure": condition == "lap.button",
                    "pair": bool(_PAIR.search(description)),
                }
            item["sets"] += sets
            weight = _step_weight_kg(step)
            if weight is not None and (item["weight"] is None or weight > item["weight"]):
                item["weight"] = weight
    return list(items.values())


def next_weight(item: dict, weight: float) -> float:
    """The smallest sensible step up from `weight` for this exercise."""
    text = f"{item.get('name') or ''} {item.get('description') or ''}".upper()
    if "KETTLEBELL" in text or item.get("category") == "CARRY":
        step = 4.0
    elif item.get("pair"):
        step = 4.0  # two dumbbells logged together: +2 kg each
    elif "DUMBBELL" in text or "DUMBELL" in text or item.get("category") in ("CURL", "LATERAL_RAISE"):
        step = 2.0
    elif weight >= 100:
        step = 5.0
    else:
        step = 2.5
    return round(weight + step, 2)


def _target(item: dict) -> Tuple[Optional[int], bool]:
    if item.get("reps"):
        return item["reps"], False
    if item.get("seconds"):
        return item["seconds"], True
    return None, False


def _hit(entry: dict, target: Optional[int], sets: int) -> bool:
    if not target:
        return False
    return sum(1 for amount in entry["done"] if amount >= target) >= max(sets, 1)


def _sets_reaching(entry: dict, weight: float, target: Optional[int]) -> int:
    """Sets done at `weight` or heavier that reached the rep/time target."""
    if not target:
        return 0
    return sum(
        1
        for set_weight, amount in entry["pairs"]
        if set_weight is not None and set_weight >= weight - 0.05 and amount is not None and amount >= target
    )


def _kg_text(weight: float, pair: bool) -> str:
    return f"{fmt_kg(weight)} kg (2×{fmt_kg(weight / 2)})" if pair else f"{fmt_kg(weight)} kg"


def recommend(item: dict, history: Sequence[Tuple[str, dict]]) -> dict:
    """What to do with a planned exercise, given its history (oldest first).

    Returns the plan and last performance as text, a status, a short action,
    the reason for it and, when the weight should change, the new weight.
    """
    plan_weight = item.get("weight")
    target, timed = _target(item)
    sets = item.get("sets") or 1
    unit = "s" if timed else ""
    scheme = f"{sets}×{target}{unit}" if target else f"{sets} sets"
    plan_text = scheme
    if plan_weight:
        plan_text += f" @ {_kg_text(plan_weight, item.get('pair'))}"
    elif item.get("to_failure"):
        plan_text = f"{sets} sets to failure"
    result = {
        "plan": plan_text,
        "plan_weight": plan_weight,
        "last": None,
        "last_date": None,
        "status": "new",
        "action": "New",
        "reason": "no history yet",
        "next_weight": None,
        "streak": 0,
    }
    if not history:
        return result

    last_date, last = history[-1]
    # Show the ramp-up too when not every planned set was at the top weight.
    result["last"] = last["top_text"] if len(last["done"]) >= sets else last["text"]
    result["last_date"] = last_date
    last_weight = last.get("top")

    if item.get("to_failure"):
        best = "·".join(str(a) for a in last["done"]) or "–"
        result.update(status="failure", action=f"Beat {best}", reason="sets to failure")
        return result

    if not plan_weight:
        if _hit(last, target, sets):
            action = "Add time or a harder variation" if timed else "Add reps or a harder variation"
            result.update(status="bodyweight", action=action, reason=f"hit {scheme}")
        else:
            result.update(status="bodyweight", action=f"Aim for {scheme}", reason="bodyweight")
        return result

    if last_weight is None or last_weight == 0:
        result.update(status="hold", action=f"Stay at {fmt_kg(plan_weight)} kg", reason="no weight logged last time")
        return result
    if plan_weight > last_weight + 0.05:
        result.update(
            status="up",
            action=f"Already up +{fmt_kg(plan_weight - last_weight)} kg",
            reason=f"from {fmt_kg(last_weight)} kg",
        )
        return result
    if plan_weight < last_weight - 0.05 and len(last["done"]) >= sets:
        result.update(
            status="below",
            action=f"Set the plan to {fmt_kg(last_weight)} kg",
            reason=f"did {len(last['done'])} sets at it last time",
            next_weight=last_weight,
        )
        return result

    streak = 0
    for _, entry in reversed(history):
        if _sets_reaching(entry, plan_weight, target) >= sets:
            streak += 1
        else:
            break
    result["streak"] = streak
    if streak:
        new_weight = next_weight(item, plan_weight)
        when = "last time" if streak == 1 else f"{streak} sessions running"
        result.update(
            status="increase",
            action=f"Go up to {_kg_text(new_weight, item.get('pair'))}",
            reason=f"hit {scheme} {when}",
            next_weight=new_weight,
        )
    else:
        result.update(status="hold", action=f"Stay at {fmt_kg(plan_weight)} kg", reason=f"aim for {scheme}")
    return result


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def _history_index(sessions: Sequence[dict]) -> Dict[Tuple[str, str], List[Tuple[str, dict]]]:
    index: Dict[Tuple[str, str], List[Tuple[str, dict]]] = {}
    for session in sorted(sessions, key=lambda s: s["date"]):
        for exercise in session["exercises"]:
            index.setdefault((session["group"], exercise["key"]), []).append((session["date"], exercise))
    return index


def plan_aliases(plans: Iterable[Tuple[str, Sequence[dict]]]) -> Dict[Tuple[str, str], str]:
    """Category-only keys to fold into a named key, per workout type.

    Garmin logs a set without an exercise name when the step had none, so the
    same movement can appear as "PUSH_UP/" and "PUSH_UP/PUSH_UP". When a
    workout type's plan names exactly one exercise in a category and has no
    unnamed step in it, unnamed history in that category is that exercise.
    """
    keys_by_group: Dict[str, set] = {}
    for group, items in plans:
        keys_by_group.setdefault(group, set()).update(item["key"] for item in items)
    aliases: Dict[Tuple[str, str], str] = {}
    for group, keys in keys_by_group.items():
        named: Dict[str, List[str]] = {}
        for key in keys:
            category, name = key.split("/", 1)
            if name:
                named.setdefault(category, []).append(key)
        for category, options in named.items():
            if len(options) == 1 and exercise_key(category, None) not in keys:
                aliases[(group, exercise_key(category, None))] = options[0]
    return aliases


def _match_history(index, group: str, item: dict):
    """History for a planned exercise, preferring the same workout type."""
    key = item["key"]
    category = item["category"]
    if (group, key) in index:
        return (group, key)
    if item.get("name") and (group, exercise_key(category, None)) in index:
        return (group, exercise_key(category, None))
    same_category = [k for k in index if k[0] == group and k[1].split("/", 1)[0] == category]
    if len(same_category) == 1:
        return same_category[0]
    others = [k for k in index if k[1] == key]
    if others:
        return max(others, key=lambda k: index[k][-1][0])
    return None


def _spark(values: Sequence[Optional[float]]) -> str:
    known = [v for v in values if v is not None]
    if not known:
        return ""
    low, high = min(known), max(known)
    out = []
    for value in values:
        if value is None:
            out.append(SPARK_GAP)
        elif high - low < 1e-9:
            out.append(SPARK_LEVELS[3])
        else:
            out.append(SPARK_LEVELS[int(round((value - low) / (high - low) * (len(SPARK_LEVELS) - 1)))])
    # Leading gaps are weeks before the exercise existed; drop them.
    return "".join(out).lstrip(SPARK_GAP)


def _progress(index, weeks: List[datetime.date], today: datetime.date, labels, plan_status, plan_order) -> dict:
    """Per-exercise progression, split by workout type plus a "retired" list.

    An exercise is current if it was done in the last 28 days; older ones are
    listed briefly under "retired" (renamed or dropped exercises).
    """
    week_pos = {week: i for i, week in enumerate(weeks)}
    cutoff_active = today - datetime.timedelta(days=28)
    entries = []
    retired = []
    for (group, key), history in index.items():
        last_date, last = history[-1]
        first_date, first = history[0]
        name = labels.get((group, key)) or last["label"]
        if datetime.date.fromisoformat(last_date) < cutoff_active:
            retired.append(
                {"group": group, "name": name, "sessions": len(history), "last_date": last_date, "now": last["top_text"]}
            )
            continue
        weekly: List[Optional[float]] = [None] * len(weeks)
        for date, entry in history:
            pos = week_pos.get(monday(datetime.date.fromisoformat(date)))
            if pos is not None and entry.get("top") is not None:
                current = weekly[pos]
                weekly[pos] = entry["top"] if current is None else max(current, entry["top"])
        top = last.get("top")
        seen_weeks = set()
        for date, entry in reversed(history):
            if not _same(entry.get("top"), top):
                break
            seen_weeks.add(monday(datetime.date.fromisoformat(date)))
        weeks_at = len(seen_weeks)
        reference = None
        for date, entry in history[:-1]:
            if datetime.date.fromisoformat(date) <= cutoff_active:
                reference = entry
        previous = history[-2][1] if len(history) > 1 else None
        weighted = bool(top)
        if not weighted:
            status = "bodyweight"
        elif previous is None:
            status = "new"
        elif previous.get("top") is not None and top > previous["top"] + 0.05:
            status = "up"
        elif previous.get("top") is not None and top < previous["top"] - 0.05:
            status = "down"
        elif weeks_at >= 3:
            status = "stalled"
        else:
            status = "hold"
        # This week's plan knows the rep targets (and whether the weight has
        # already been raised), so its verdict wins.
        status = plan_status.get((group, key), status)
        entry = {
            "id": f"{group}:{key}",
            "name": name,
            "sessions": len(history),
            "first_date": first_date,
            "first_weight": first.get("top"),
            "last_date": last_date,
            "weight": top,
            "now": last["top_text"],
            "e1rm": last.get("e1rm"),
            "change_4w": round(top - reference["top"], 2)
            if weighted and reference and reference.get("top") is not None
            else None,
            "change_start": round(top - first["top"], 2)
            if weighted and first.get("top") is not None and len(history) > 1
            else None,
            "weeks_at_weight": weeks_at if weighted else None,
            "status": status,
            "spark": _spark(weekly) if weighted else "",
        }
        if weighted:
            entry["weekly"] = weekly
        entries.append((group, plan_order.get((group, key), 999), entry))

    entries.sort(key=lambda e: (e[1], -e[2]["sessions"], e[2]["name"]))
    retired.sort(key=lambda r: r["last_date"], reverse=True)
    week_list = [w.isoformat() for w in weeks]
    out: dict = {}
    for group in GROUP_ORDER:
        # Every group is always present so dashboard sensors keep a stable shape.
        out[group.lower()] = {"weeks": week_list, "exercises": [e for g, _, e in entries if g == group]}
    out["retired"] = retired
    return out


def _pack(items: List[dict], parts: int, part_bytes: int) -> Tuple[List[List[dict]], int]:
    """Split the log into parts that each stay under `part_bytes` of JSON."""
    packed: List[List[dict]] = [[] for _ in range(parts)]
    sizes = [2] * parts
    current = 0
    dropped = 0
    for item in items:
        size = len(json.dumps(item, ensure_ascii=False).encode("utf-8")) + 1
        while current < parts and sizes[current] + size > part_bytes and packed[current]:
            current += 1
        if current >= parts:
            dropped += 1
            continue
        packed[current].append(item)
        sizes[current] += size
    return packed, dropped


def build_report(
    activities: Sequence[dict],
    sets_by_id: Dict[Any, Optional[dict]],
    schedule: Sequence[dict],
    workouts: Dict[Any, dict],
    today: datetime.date,
    days: int,
    log_parts: int = 3,
    log_part_bytes: int = 14000,
) -> dict:
    """Everything the dashboard needs, from already-fetched Garmin data."""
    since = today - datetime.timedelta(days=days)
    this_monday = monday(today)
    this_sunday = this_monday + datetime.timedelta(days=6)

    # This week's loaded strength workouts (mobility and the like are skipped).
    plans = []
    for entry in sorted(schedule or [], key=lambda s: str(s.get("scheduleDate") or "")):
        workout = workouts.get(entry.get("workoutId"))
        if not workout:
            continue
        items = plan_items(workout)
        if not any(item.get("weight") for item in items):
            continue
        name = entry.get("workoutName") or workout.get("workoutName") or "Workout"
        plans.append((entry, name, workout_group(name), items))
    aliases = plan_aliases((group, items) for _, _, group, items in plans)

    sessions = []
    for activity in activities:
        session = summarise_session(activity, sets_by_id.get(activity.get("activityId")))
        if session["date"] and since.isoformat() <= session["date"] <= today.isoformat() and session["exercises"]:
            for exercise in session["exercises"]:
                exercise["key"] = aliases.get((session["group"], exercise["key"]), exercise["key"])
            sessions.append(session)
    sessions.sort(key=lambda s: (s["date"], s["id"] or 0), reverse=True)
    index = _history_index(sessions)

    labels: Dict[Tuple[str, str], str] = {}
    plan_status: Dict[Tuple[str, str], str] = {}
    plan_order: Dict[Tuple[str, str], int] = {}
    this_week = []
    for entry, name, group, items in plans:
        rows = []
        for item in items:
            matched = _match_history(index, group, item)
            history = index.get(matched, []) if matched else []
            advice = recommend(item, history)
            if matched:
                labels.setdefault(matched, item["label"])
                plan_order.setdefault(matched, len(plan_order))
                if advice["status"] in ("increase", "below", "up"):
                    plan_status[matched] = advice["status"]
            rows.append(
                {
                    "exercise": item["label"],
                    **{
                        k: advice[k]
                        for k in ("plan", "last", "last_date", "status", "action", "reason", "next_weight")
                    },
                }
            )
        when = datetime.date.fromisoformat(str(entry.get("scheduleDate"))[:10])
        this_week.append(
            {
                "date": when.isoformat(),
                "day": day_label(when),
                "workout": name,
                "group": group,
                "workout_id": entry.get("workoutId"),
                "done": entry.get("associatedActivityId") is not None,
                "items": rows,
            }
        )

    # Weekly volume.
    first_week = monday(since)
    weeks: List[datetime.date] = []
    cursor = first_week
    while cursor <= this_monday:
        weeks.append(cursor)
        cursor += datetime.timedelta(days=7)
    weekly = {week: {"week": week.isoformat(), "sessions": 0, "sets": 0, "volume": 0, **{g.lower(): 0 for g in GROUP_ORDER}} for week in weeks}
    for session in sessions:
        week = monday(datetime.date.fromisoformat(session["date"]))
        if week in weekly:
            row = weekly[week]
            row["sessions"] += 1
            row["sets"] += session["sets"]
            row["volume"] += session["volume"]
            row[session["group"].lower()] += session["volume"]

    progress = _progress(index, weeks, today, labels, plan_status, plan_order)
    active = [e for group in GROUP_ORDER for e in progress[group.lower()]["exercises"]]

    def label_for(session, exercise):
        return labels.get((session["group"], exercise["key"])) or exercise["label"]

    log = [
        {
            "date": s["date"],
            "day": day_label(datetime.date.fromisoformat(s["date"])),
            "name": s["name"],
            "minutes": s["minutes"],
            "sets": s["sets"],
            "volume": s["volume"],
            "exercises": [[label_for(s, e), e["text"]] for e in s["exercises"]],
        }
        for s in sessions
    ]
    parts, dropped = _pack(log, max(log_parts, 1), log_part_bytes)

    last_week = this_monday - datetime.timedelta(days=7)
    complete = [weekly[w]["volume"] for w in weeks if w < this_monday][-4:]
    statuses = [row["status"] for workout in this_week for row in workout["items"]]
    summary = {
        "sessions": len(sessions),
        "sessions_this_week": weekly[this_monday]["sessions"] if this_monday in weekly else 0,
        "sessions_last_week": weekly[last_week]["sessions"] if last_week in weekly else 0,
        "planned_this_week": len(this_week),
        "done_this_week": sum(1 for w in this_week if w["done"]),
        "increases_due": statuses.count("increase"),
        "plan_below_last": statuses.count("below"),
        "stalled": sum(1 for p in active if p["status"] == "stalled"),
        "progressing_4w": sum(1 for p in active if (p["change_4w"] or 0) > 0),
        "exercises_active": len(active),
        "volume_this_week": weekly[this_monday]["volume"] if this_monday in weekly else 0,
        "volume_last_week": weekly[last_week]["volume"] if last_week in weekly else 0,
        "volume_4w_avg": int(round(sum(complete) / len(complete))) if complete else 0,
        "log_dropped": dropped,
    }

    return {
        "date": today.isoformat(),
        "range": {"start": since.isoformat(), "end": today.isoformat(), "days": days},
        "week": {"start": this_monday.isoformat(), "end": this_sunday.isoformat()},
        "summary": summary,
        "this_week": this_week,
        "weeks": [weekly[w] for w in weeks],
        "progress": progress,
        "log": {f"part{i + 1}": part for i, part in enumerate(parts)},
    }
