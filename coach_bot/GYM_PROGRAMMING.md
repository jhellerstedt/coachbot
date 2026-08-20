# How your gym sessions are built

A plain-language guide to how the coach bot constructs and progresses your gym work.

## The big idea: train with intent

Gym work is about **intent**, not just moving weight:

- **Move the bar fast.** Every working rep aims for maximal velocity on the concentric (lifting) phase — explosive, with quality and control.
- **Quality over quantity.** We don't train to failure. Stop a set while bar speed and technique are still crisp.
- **Lightweight rowers (≈60–75 kg)** train mainly for *neural* adaptation: low volume (3–5 reps), high intensity (>85% of 1RM), and longer rest between sets — strength and power without unnecessary bulk.

## When you lift

A fixed weekly rhythm keeps gym and rowing complementary:

- **Monday AM** – gym
- **Tuesday AM** – erg
- **Wednesday AM** – gym
- **Thursday AM** – rowing on water *or* erg
- **Friday–Sunday** – rest or optional light recovery

## What a session looks like

- **Compound lifts first**, then core/accessory work.
- Every working set is done with **intent** — explosive concentric speed and clean technique, kept short of failure.
- Every exercise is given a clear **goal label**: strength, hypertrophy, power, or recovery.
- Each set is written out individually with **reps and a target weight in kg** (e.g. `Set 1: 6×60 kg`, `Set 2: 6×70 kg`, `Set 3: 6×80 kg`) — no vague "4 sets of 6," no "% 1RM" without a kg number. Bodyweight or timed moves (pull-ups, plank) still get reps or a duration per set.

## The A/B split: two session types

The two gym days are deliberately different so you train the whole body across the week:

- **Day A — leg / posterior-chain dominant** (e.g. back squat, hex-bar / Romanian deadlift, Bulgarian split squat, kettlebell swings).
- **Day B — upper-body / core dominant** (e.g. bench / incline press, barbell row, lat pull-down, pull-ups, Arnold press, Russian twists, plank).

Monday and Wednesday each take one of these roles; they're never collapsed into the same type.

## The gym program (source of truth)

A **mesocycle program** owns which four exercises you do on Monday (leg) and Wednesday (upper/core). The weekly plan *materializes* that program — it does not invent a new menu each week.

Planned rotations (for example swapping Bulgarian split squat for kettlebell swings after a few weeks) live on the program. Deload weeks **reuse last week's exercise names** and drop to two lighter sets.

## Squad plan vs your personal plan

- The **public squad plan** uses the program's exercises. Squad kg are the **median** of each athlete's latest logged peak for that lift, then the usual phase pyramid (base ascending / build reverse).
- Your **private DM plan** uses the **exact same exercises** in the same order. Your kg come from **your last logs plus that lift's progression rule** (typically +2.5 kg after two sessions at the top of the range), then the same pyramid.

## How the load is decided (periodisation)

Gym intensity is matched to where you are in the season:

- **Early season (base)** → hypertrophy and durability (ascending pyramids).
- **Build / pre-championships** → strength and power (reverse pyramids).
- **Season deload weeks** are prescribed on the calendar: same exercises, two sets at about 82% of working weight.

**Load management:** heavy leg days (back squat / hex-bar) are kept away from high-intensity erg sessions (2k-pace / anaerobic work) by at least 24 hours.

## Progressive overload (per lift, from your logs)

- Every time you log a gym session, **tonnage** is still recorded (sum of reps × weight). Pull-ups and other bodyweight moves are counted at your body weight; unilateral lifts like Bulgarian split squats count both legs.
- **Each lift** also has a progression rule. After you log:
  - Hit the target reps with RPE ≤ 7 (or no RPE given) for two sessions running → add 2.5 kg next week.
  - Hit the reps but grind (RPE 9–10) → **hold**.
  - Miss the reps → **hold or drop 2.5 kg** on that lift only.
- Season deload weeks stay prescribed. Per-lift hold/progress is inferred from logs on working weeks.

If you log sets without an effort rating, the bot will ask once for **RPE 1–10** (or easy / moderate / hard / max).

When you log a session, the bot still benchmarks **actual vs prescribed** tonnage and **vs your last comparable day** (leg vs leg, upper/core vs upper/core).

## Today only: recovery gate

If you say recovery is poor (bad sleep, wrecked, low HRV), **today's** gym reply can drop a set or about 10% load. That does **not** rewrite the weekly program.

## Season targets it all feeds

- **Head of the Yarra** (Nov) – 8.5 km eights: aerobic endurance and durability.
- **Victoria State Championships** – 2 km 4x−, target crew time 6:40: strength and power.
