"""Tests for plan_text_import athlete markdown."""

from __future__ import annotations

from plan_text_import import import_weekly_plan_json_from_text
from weekly_plan_schema import parse_weekly_plan, validate_fixed_weekly_schedule


ATHLETE_BUILD_PROSE = """Jack H

**Monday, 2026-07-20**
*Session Type: Gym (Leg/Posterior Chain)*
1. Back squat
Set 1: 8×85 kg
Set 2: 8×85 kg
Set 3: 8×85 kg
2. Romanian deadlift
Set 1: 8×62.5 kg
Set 2: 8×62.5 kg
Set 3: 8×62.5 kg
3. Bulgarian split squat
Set 1: 8×35 kg
Set 2: 8×35 kg
Set 3: 8×35 kg
4. Kettlebell swings
Set 1: 10×20 kg
Set 2: 10×20 kg
Set 3: 10×20 kg

**Tuesday, 2026-07-21**
*Session Type: Erg*
Warm-up: 12 min @ Z2/T3, split 2:10–2:05, HR 125–135 bpm, priority: HR
Main Set: 2×2000 m / 5 min rest @ Z4/T6, split 1:55–2:05, HR 140–150 bpm, priority: HR
Cool-down: 10 min @ Z1/T1, split 2:15–2:05, HR 120–125 bpm, priority: HR

**Wednesday, 2026-07-22**
*Session Type: Gym (Upper Body/Core)*
1. Bench press
Set 1: 8×50 kg
Set 2: 8×55 kg
Set 3: 8×55 kg
2. Barbell row
Set 1: 8×45 kg
Set 2: 8×50 kg
Set 3: 8×50 kg
3. Lat pull-down
Set 1: 8×90 kg
Set 2: 8×95 kg
Set 3: 8×95 kg
4. Plank
Set 1: 30s hold
Set 2: 30s hold
Set 3: 30s hold

**Thursday, 2026-07-23**
*Session Type: On Water / Erg Alternative*
Warm-up: 12 min @ Z2/T3, split 2:10–2:05, HR 125–135 bpm, priority: HR
Main Set: 3×1500 m / 3 min rest @ Z3/T4, split 2:05–2:15, HR 130–140 bpm, priority: HR
Cool-down: 10 min @ Z1/T1, split 2:15–2:10, HR 120–125 bpm, priority: HR

**Friday, 2026-07-24**
*Session Type: Rest*

**Saturday, 2026-07-25**
*Session Type: Rest*

**Sunday, 2026-07-26**
*Session Type: Rest*
"""


def test_import_athlete_markdown_prose():
    data = import_weekly_plan_json_from_text(
        ATHLETE_BUILD_PROSE,
        week_start="2026-07-20",
        personalised=True,
        greeting="Jack H",
    )
    assert data is not None
    plan = parse_weekly_plan(data)
    assert plan is not None
    assert validate_fixed_weekly_schedule(plan) is None
    mon = next(d for d in plan.days if d.weekday == "Monday")
    wed = next(d for d in plan.days if d.weekday == "Wednesday")
    assert mon.gym is not None and mon.gym.category == "leg"
    assert wed.gym is not None and wed.gym.category == "upper_core"
    tue = next(d for d in plan.days if d.weekday == "Tuesday")
    assert len(tue.rowing.segments) >= 3
