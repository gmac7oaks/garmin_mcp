"""Strength-training fixtures: real exercise sets plus workout plans.

strength_exercise_sets.json holds four real /activity/{id}/exerciseSets
responses (trimmed to the fields the report reads): Push 22 Jul, Push 23 Sep,
Push v2 27 Oct and Legs v2 28 Oct 2026. The plans below mirror the Push v3,
Legs v2 and mobility workouts scheduled for the week of 2 Nov 2026, in
Garmin's raw workout-service format.
"""

import json
from pathlib import Path

_SETS_FILE = Path(__file__).with_name("strength_exercise_sets.json")


def load_sets() -> dict:
    """Exercise-set responses keyed by activity id (int)."""
    raw = json.loads(_SETS_FILE.read_text())
    return {int(k): v for k, v in raw.items()}


def ex(category, name=None, weight=None, reps=None, seconds=None, lap=False, desc="", step_type="interval", unit="kilogram"):
    step = {
        "type": "ExecutableStepDTO",
        "stepType": {"stepTypeKey": step_type},
        "category": category,
        "description": desc,
    }
    if name:
        step["exerciseName"] = name
    if weight is not None:
        step["weightValue"] = float(weight)
        step["weightUnit"] = {"unitKey": unit}
    if reps:
        step["endCondition"] = {"conditionTypeKey": "reps"}
        step["endConditionValue"] = float(reps)
    elif seconds:
        step["endCondition"] = {"conditionTypeKey": "time"}
        step["endConditionValue"] = float(seconds)
    elif lap:
        step["endCondition"] = {"conditionTypeKey": "lap.button"}
    return step


def rest(seconds=60):
    return {
        "type": "ExecutableStepDTO",
        "stepType": {"stepTypeKey": "rest"},
        "endCondition": {"conditionTypeKey": "time"},
        "endConditionValue": float(seconds),
    }


def repeat(times, *steps):
    return {
        "type": "RepeatGroupDTO",
        "stepType": {"stepTypeKey": "repeat"},
        "numberOfIterations": times,
        "workoutSteps": list(steps),
    }


def workout(name, *steps):
    return {"workoutName": name, "workoutSegments": [{"workoutSteps": list(steps)}]}


PUSH_V3 = workout(
    "Push v3",
    ex("ROW", "INDOOR_ROW", desc="5 min easy row", step_type="warmup", lap=True),
    rest(),
    ex("BENCH_PRESS", "INCLINE_DUMBBELL_BENCH_PRESS", 36, reps=12, desc="Warm-up set", step_type="warmup"),
    rest(60),
    repeat(3, ex("BENCH_PRESS", "INCLINE_DUMBBELL_BENCH_PRESS", 64, reps=8, desc="6-8 reps, 2x32kg"), rest(120)),
    repeat(3, ex("BENCH_PRESS", None, 72.5, reps=8, desc="Flat bench, 6-8 reps"), rest(120)),
    repeat(2, ex("FLYE", None, 75, reps=12, desc="Machine fly, 10-12 reps"), rest(75)),
    repeat(3, ex("SHOULDER_PRESS", "OVERHEAD_DUMBBELL_PRESS", 18, reps=10, desc="Seated, 8-10 reps"), rest(90)),
    repeat(3, ex("LATERAL_RAISE", None, 10, reps=15, desc="Seated, 12-15 reps"), rest(60)),
    repeat(3, ex("CURL", None, 20, reps=12, desc="Alternating dumbbell curls, 10-12 reps"), rest(60)),
    repeat(2, ex("PUSH_UP", "PUSH_UP", lap=True, desc="To near failure - press lap when done"), rest(60)),
)

LEGS_V2 = workout(
    "Legs v2",
    ex("ROW", "INDOOR_ROW", desc="5 min easy row", step_type="warmup", lap=True),
    repeat(4, ex("PLYO", "BOX_JUMP", reps=10, desc="Max intent, step down"), rest(60)),
    repeat(3, ex("SQUAT", "BARBELL_BACK_SQUAT", 95, reps=8, desc="Perfect Squat Machine, 6-8 reps"), rest(120)),
    repeat(3, ex("SQUAT", "LEG_PRESS", 73, reps=8, desc="Single leg - 8 each leg"), rest(90)),
    repeat(
        3,
        ex(
            "DEADLIFT",
            "SINGLE_LEG_ROMANIAN_DEADLIFT_WITH_DUMBBELL",
            20,
            reps=8,
            desc="8 each leg, kettlebell in opposite hand",
        ),
        rest(90),
    ),
    repeat(3, ex("HIP_RAISE", "BARBELL_HIP_THRUST_ON_FLOOR", 45, reps=8), rest(90)),
    repeat(3, ex("LEG_CURL", "WEIGHTED_LEG_CURL", 39, reps=8, desc="Single leg - 8 each leg"), rest(90)),
    repeat(3, ex("CALF_RAISE", "WEIGHTED_SEATED_CALF_RAISE", 134, reps=12, desc="10-12 reps, slow lower"), rest(75)),
)

MOBILITY = workout(
    "Sunday Runner Mobility (optional)",
    ex("WARM_UP", desc="5 min easy bike", step_type="warmup", lap=True),
    repeat(2, ex("HIP_RAISE", reps=10, desc="Single-leg glute bridge - 10 each leg, bodyweight"), rest(30)),
)

WORKOUTS = {1743815609: PUSH_V3, 1716968605: LEGS_V2, 1729230803: MOBILITY}


def _activity(activity_id, name, start, duration, type_key="strength_training"):
    return {
        "activityId": activity_id,
        "activityName": name,
        "startTimeLocal": start,
        "duration": duration,
        "activityType": {"typeKey": type_key},
    }


ACTIVITIES = [
    _activity(24877545501, "Legs v2", "2026-10-28 05:38:43", 3701.6),
    _activity(24865183793, "Push v2", "2026-10-27 05:37:10", 3814.6),
    _activity(24464818941, "Wednesday's Push", "2026-09-23 05:47:41", 2236.0),
    _activity(24279298293, "Indoor Rowing", "2026-09-08 06:21:58", 579.8, "indoor_rowing"),
    _activity(23686126228, "Push — Progression (w/c 20 Jul", "2026-07-22 05:33:01", 3567.3),
]

SCHEDULE = [
    {"scheduleDate": "2026-11-03", "workoutId": 1739208647, "workoutName": "Marden W5 - 3x3km race pace", "workoutType": "running"},
    {"scheduleDate": "2026-11-03", "workoutId": 1743815609, "workoutName": "Push v3", "workoutType": "strength_training", "associatedActivityId": None},
    {"scheduleDate": "2026-11-04", "workoutId": 1716968605, "workoutName": "Legs v2", "workoutType": "strength_training"},
    {"scheduleDate": "2026-11-08", "workoutId": 1729230803, "workoutName": "Sunday Runner Mobility (optional)", "workoutType": "strength_training"},
]
