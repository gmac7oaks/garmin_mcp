"""Tests for the strength-training report (garmin_mcp.strength)."""
import datetime

import pytest

from garmin_mcp import strength
from tests.fixtures.strength_data import (
    ACTIVITIES,
    LEGS_V2,
    PUSH_V3,
    SCHEDULE,
    WORKOUTS,
    ex,
    load_sets,
    repeat,
    rest,
    workout,
)

TODAY = datetime.date(2026, 11, 2)  # a Monday


def raw_set(category, name, kg, reps=None, seconds=30.0, set_type="ACTIVE"):
    return {
        "exercises": [{"category": category, "name": name, "probability": 99.6}],
        "duration": seconds,
        "repetitionCount": reps,
        "weight": None if kg is None else kg * 1000.0,
        "setType": set_type,
    }


def activity(activity_id, name, start):
    return {"activityId": activity_id, "activityName": name, "startTimeLocal": start, "duration": 3000.0}


@pytest.fixture(scope="module")
def report():
    return strength.build_report(ACTIVITIES, load_sets(), SCHEDULE, WORKOUTS, TODAY, 120)


def test_parse_sets_keeps_lifting_sets_only():
    sets = strength.parse_sets(load_sets()[24865183793])  # Push v2, 27 Oct
    keys = [s["key"] for s in sets]
    assert "ROW/INDOOR_ROW" not in keys  # warm-up row
    assert len(sets) == 20
    first = sets[0]
    assert first == {
        "key": "BENCH_PRESS/INCLINE_DUMBBELL_BENCH_PRESS",
        "category": "BENCH_PRESS",
        "name": "INCLINE_DUMBBELL_BENCH_PRESS",
        "weight": 36.0,
        "reps": 12,
        "seconds": 44,
    }


def test_parse_sets_drops_unknown_and_empty_sets():
    data = {
        "exerciseSets": [
            raw_set("UNKNOWN", None, None, reps=0, seconds=3.0),
            raw_set("BENCH_PRESS", None, 60, reps=8),
            raw_set("BENCH_PRESS", None, None, set_type="REST"),
            {"exercises": [], "setType": "ACTIVE", "repetitionCount": 5},
        ]
    }
    assert [s["key"] for s in strength.parse_sets(data)] == ["BENCH_PRESS/"]


def test_session_summary_from_real_sets():
    session = strength.summarise_session(ACTIVITIES[1], load_sets()[24865183793])
    assert session["date"] == "2026-10-27"
    assert session["group"] == "Push"
    assert session["minutes"] == 64
    assert session["sets"] == 20
    assert session["volume"] == 6914
    incline = session["exercises"][0]
    assert incline["text"] == "36 kg × 12, 60 kg × 8·8·8"
    assert incline["top"] == 60.0
    assert incline["done"] == [8, 8, 8]
    assert incline["top_text"] == "60 kg × 8·8·8"
    assert incline["volume"] == 36 * 12 + 60 * 24
    assert session["exercises"][-1]["text"] == "BW × 27·18"


def test_format_sets_groups_by_weight_and_handles_timed_sets():
    rdl = [
        {"weight": 16.0, "reps": 8},
        {"weight": 16.0, "reps": 8},
        {"weight": 20.0, "reps": 8},
    ]
    assert strength.format_sets(rdl) == "16 kg × 8·8, 20 kg × 8"
    carry = [{"weight": 24.0, "reps": None, "seconds": 40}] * 2
    assert strength.format_sets(carry) == "24 kg × 40s·40s"


@pytest.mark.parametrize(
    "description, expected",
    [
        ("Flat bench, 6-8 reps", "Flat bench"),
        ("Machine fly, 10-12 reps", "Machine fly"),
        ("Pallof press - 10 per side, 2s hold", "Pallof press"),
        ("Tibialis raises 15-20, dumbbell between feet", "Tibialis raises"),
        ("Seated, 8-10 reps", None),
        ("To near failure - press lap when done", None),
        ("Single leg - 8 each leg", None),
        ("6-8 reps, 2x32kg", None),
        ("", None),
    ],
)
def test_label_from_description(description, expected):
    assert strength.label_from_description(description) == expected


def test_workout_group():
    assert strength.workout_group("Wednesday's Push") == "Push"
    assert strength.workout_group("Pull v3") == "Pull"
    assert strength.workout_group("Tuesday's Legs") == "Legs"
    assert strength.workout_group("Saturday's Core v2") == "Core"
    assert strength.workout_group("Strength") == "Other"


def test_plan_items_skip_warmups_and_read_targets():
    items = {i["key"]: i for i in strength.plan_items(PUSH_V3)}
    assert "ROW/INDOOR_ROW" not in items
    incline = items["BENCH_PRESS/INCLINE_DUMBBELL_BENCH_PRESS"]
    assert incline["sets"] == 3  # the warm-up set is not counted
    assert incline["weight"] == 64.0
    assert incline["reps"] == 8
    assert incline["rep_min"] == 6
    assert incline["pair"] is True
    assert items["BENCH_PRESS/"]["label"] == "Flat bench"
    assert items["LATERAL_RAISE/"]["label"] == "Lateral raise"
    assert items["PUSH_UP/PUSH_UP"]["to_failure"] is True


def test_plan_items_merge_sides_and_convert_pounds():
    plan = workout(
        "Core",
        repeat(
            3,
            ex("CARRY", "FARMERS_CARRY", 24, seconds=40, desc="Suitcase carry - LEFT hand"),
            rest(10),
            ex("CARRY", "FARMERS_CARRY", 24, seconds=40, desc="Suitcase carry - RIGHT hand"),
        ),
        repeat(2, ex("PULL_UP", "LAT_PULLDOWN", 130, reps=8, unit="pound")),
    )
    items = {i["key"]: i for i in strength.plan_items(plan)}
    assert items["CARRY/FARMERS_CARRY"]["sets"] == 6
    assert items["CARRY/FARMERS_CARRY"]["seconds"] == 40
    assert items["CARRY/FARMERS_CARRY"]["label"] == "Suitcase carry"
    assert items["PULL_UP/LAT_PULLDOWN"]["weight"] == 59.0


@pytest.mark.parametrize(
    "item, weight, expected",
    [
        ({"pair": True, "description": "2x32kg"}, 64, 68),
        ({"name": "SINGLE_LEG_ROMANIAN_DEADLIFT_WITH_DUMBBELL", "description": "kettlebell in opposite hand"}, 20, 24),
        ({"name": "OVERHEAD_DUMBBELL_PRESS"}, 18, 20),
        ({"category": "LATERAL_RAISE"}, 10, 12),
        ({"name": "WEIGHTED_SEATED_CALF_RAISE"}, 134, 139),
        ({"name": "LEG_PRESS"}, 73, 75.5),
    ],
)
def test_next_weight_steps(item, weight, expected):
    assert strength.next_weight(item, weight) == expected


def _history(*entries):
    return [(date, strength.summarise_exercise(sets)) for date, sets in entries]


def test_recommend_increase_once_and_running():
    item = {"key": "SQUAT/LEG_PRESS", "category": "SQUAT", "name": "LEG_PRESS", "weight": 73.0, "sets": 3, "reps": 8}
    done = [{"weight": 73.0, "reps": 8}] * 3
    once = strength.recommend(item, _history(("2026-10-21", [{"weight": 70.0, "reps": 8}] * 3), ("2026-10-28", done)))
    assert once["status"] == "increase"
    assert once["next_weight"] == 75.5
    assert once["action"] == "Go up to 75.5 kg (hit 3×8 once)"
    twice = strength.recommend(item, _history(("2026-10-21", done), ("2026-10-28", done)))
    assert twice["streak"] == 2
    assert "2 sessions running" in twice["action"]


def test_recommend_hold_up_below_and_new():
    item = {"key": "DEADLIFT/X", "category": "DEADLIFT", "name": "X", "weight": 20.0, "sets": 3, "reps": 8}
    ramped = _history(("2026-10-28", [{"weight": 16.0, "reps": 8}, {"weight": 16.0, "reps": 8}, {"weight": 20.0, "reps": 8}]))
    hold = strength.recommend(item, ramped)
    assert hold["status"] == "hold"
    assert hold["last"] == "20 kg × 8"
    assert hold["action"] == "Stay at 20 kg until you hit 3×8"

    up = strength.recommend({**item, "weight": 24.0}, ramped)
    assert (up["status"], up["action"]) == ("up", "Already up +4 kg")

    below = strength.recommend({**item, "weight": 17.0}, _history(("2026-10-24", [{"weight": 22.0, "reps": 10}] * 3)))
    assert below["status"] == "below"
    assert below["next_weight"] == 22.0

    assert strength.recommend(item, [])["status"] == "new"


def test_recommend_bodyweight_and_failure_sets():
    pullups = {"key": "PULL_UP/PULL_UP", "category": "PULL_UP", "name": "PULL_UP", "weight": None, "sets": 3, "reps": 8}
    hit = strength.recommend(pullups, _history(("2026-10-29", [{"weight": 0.0, "reps": 8}] * 3)))
    assert hit["status"] == "bodyweight"
    assert hit["action"].startswith("Hit every set")
    pushups = {"key": "PUSH_UP/PUSH_UP", "category": "PUSH_UP", "weight": None, "sets": 2, "to_failure": True}
    fail = strength.recommend(pushups, _history(("2026-10-27", [{"weight": 0.0, "reps": 27}, {"weight": 0.0, "reps": 18}])))
    assert (fail["status"], fail["action"], fail["plan"]) == ("failure", "Beat 27·18", "2 sets to failure")


def test_report_this_week_uses_the_scheduled_plans(report):
    workouts = report["this_week"]
    assert [(w["day"], w["workout"]) for w in workouts] == [("Tue 3 Nov", "Push v3"), ("Wed 4 Nov", "Legs v2")]
    push = {row["exercise"]: row for row in workouts[0]["items"]}
    assert push["Incline dumbbell bench press"]["plan"] == "3×8 @ 64 kg (2×32)"
    assert push["Incline dumbbell bench press"]["action"] == "Already up +4 kg"
    assert push["Lateral raise"]["status"] == "increase"
    assert push["Lateral raise"]["next_weight"] == 12.0
    legs = {row["exercise"]: row for row in workouts[1]["items"]}
    assert legs["Perfect Squat Machine"]["status"] == "up"
    assert legs["Single leg romanian deadlift with dumbbell"]["status"] == "hold"
    increases = sorted(r["exercise"] for w in workouts for r in w["items"] if r["status"] == "increase")
    assert increases == [
        "Barbell hip thrust on floor",
        "Lateral raise",
        "Leg press",
        "Weighted leg curl",
        "Weighted seated calf raise",
    ]
    assert report["summary"]["increases_due"] == 5
    assert report["summary"]["planned_this_week"] == 2


def test_report_progress_tracks_load_over_time(report):
    push = {e["name"]: e for e in report["progress"]["push"]["exercises"]}
    incline = push["Incline dumbbell bench press"]
    assert incline["sessions"] == 3
    assert (incline["first_date"], incline["first_weight"]) == ("2026-07-22", 56.0)
    assert incline["weight"] == 60.0
    assert incline["change_start"] == 4.0
    assert incline["change_4w"] == 4.0  # vs 23 Sep
    assert incline["status"] == "up"
    weeks = report["progress"]["push"]["weeks"]
    assert weeks[0] == "2026-06-29" and weeks[-1] == "2026-11-02"
    assert incline["weekly"][weeks.index("2026-10-26")] == 60.0
    assert incline["weekly"][weeks.index("2026-09-21")] == 56.0
    assert incline["spark"].startswith("▁") and incline["spark"].endswith("█·")
    assert push["Lateral raise"]["status"] == "increase"  # the plan's verdict wins
    # The plan's order leads the table; July-only exercises are retired.
    assert list(push)[:2] == ["Incline dumbbell bench press", "Flat bench"]
    retired = {r["name"] for r in report["progress"]["retired"]}
    assert "Smith machine bench press" in retired
    assert set(report["progress"]) == {"push", "pull", "legs", "core", "other", "retired"}


def test_report_weeks_and_log(report):
    last_week = next(w for w in report["weeks"] if w["week"] == "2026-10-26")
    assert last_week["sessions"] == 2
    assert last_week["push"] == 6914
    assert last_week["legs"] == 11168
    assert last_week["volume"] == 6914 + 11168
    log = report["log"]["part1"]
    assert [s["date"] for s in log] == ["2026-10-28", "2026-10-27", "2026-09-23", "2026-07-22"]
    assert log[1]["exercises"][1] == ["Flat bench", "70 kg × 8·8·8"]
    assert report["log"]["part2"] == [] and report["log"]["part3"] == []
    assert report["summary"]["sessions"] == 4  # the rowing session is not strength
    assert report["week"] == {"start": "2026-11-02", "end": "2026-11-08"}


def test_log_is_split_to_fit_attribute_limits():
    sessions = [{"n": i, "pad": "x" * 100} for i in range(10)]
    parts, dropped = strength._pack(sessions, parts=2, part_bytes=400)
    assert [len(p) for p in parts] == [3, 3]
    assert dropped == 4


def test_progress_status_stalled_and_down():
    def session(activity_id, date, kg, reps):
        return activity(activity_id, "Pull", f"{date} 06:00:00"), {
            "exerciseSets": [raw_set("ROW", "BENT_OVER_ROW_WITH_DUMBELL", kg, reps=r) for r in reps]
        }

    data = [
        session(1, "2026-10-08", 36, [8, 8, 7]),
        session(2, "2026-10-15", 36, [8, 8, 8]),
        session(3, "2026-10-22", 36, [8, 8, 8]),
        session(4, "2026-10-29", 36, [8, 8, 8]),
    ]
    acts = [a for a, _ in data]
    sets = {a["activityId"]: s for a, s in data}
    report = strength.build_report(acts, sets, [], {}, TODAY, 60)
    row = report["progress"]["pull"]["exercises"][0]
    assert row["name"] == "Bent over row with dumbbell"
    assert row["weeks_at_weight"] == 4
    assert row["status"] == "stalled"
    assert report["summary"]["stalled"] == 1

    data.append(session(5, "2026-11-01", 32, [8, 8, 8]))
    acts = [a for a, _ in data]
    sets = {a["activityId"]: s for a, s in data}
    report = strength.build_report(acts, sets, [], {}, TODAY, 60)
    assert report["progress"]["pull"]["exercises"][0]["status"] == "down"


def test_report_skips_unloaded_workouts_and_tolerates_missing_sets():
    report = strength.build_report(ACTIVITIES, {}, SCHEDULE, WORKOUTS, TODAY, 120)
    assert report["summary"]["sessions"] == 0
    assert [w["workout"] for w in report["this_week"]] == ["Push v3", "Legs v2"]
    assert {r["status"] for w in report["this_week"] for r in w["items"]} == {"new"}
