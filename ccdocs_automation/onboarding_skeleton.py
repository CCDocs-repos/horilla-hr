"""The onboarding stage/task skeleton for a call-center class, as data.

WHY THIS FILE EXISTS
--------------------
Horilla ships an onboarding module (stages, tasks, per-candidate task status,
a kanban board) and CCDocs has never used it. Measured 2026-08-17 09:09 EDT on
horilla-db: `onboarding_onboardingtask` = 0, `onboarding_candidatestage` = 0,
`onboarding_candidatetask` = 0, `onboarding_onboardingportal` = 0,
`recruitment_candidate WHERE start_onboard` = 0. The only rows that exist are 7
auto-created stages all literally named "Initial" (one per recruitment, from the
`create_initial_stage` post_save on Recruitment) -- and recruitment 12, the
Director req, does not even have that.

So a 30-35 person class has no path. This module is the path, expressed as
declarative data so it can be diffed, reviewed, and re-applied idempotently
instead of being clicked into a UI 35 times.

DESIGN DECISIONS, and why
-------------------------
1. We RENAME the existing "Initial" stage into Day 0 rather than inserting a new
   stage in front of it. Two consumers pick the entry stage as
   `onboarding_stage.first()` / `.order_by("sequence")[0]` (onboarding/views.py
   `email_send` and `candidates_single_view`), and Meta ordering is `["sequence"]`
   -- so the lowest-sequence stage IS the entry point. Adding Day 0 at sequence 1
   would leave every new hire parked in an empty stage named "Initial". Renaming
   sequence 0 is the smaller, correct change, and it destroys nothing: the
   "Initial" stages hold 0 candidates.

2. Two tracks. The agent track (recruitments 2 and 6) graduates on the 3
   CAA/day standard. The director track (recruitment 12) does not -- a Director
   does not dial, so cloning "hit 3 CAA/day" onto them would be a task nobody
   can ever tick. Directors graduate when THEIR TEAM holds the standard.

3. Owners are Employee FKs, so they must exist. Measured: Jose Rojas Hernandez
   = employee 197, William Torres = 168, Paola Palacios = 172. Owners are
   resolved BY EMAIL at apply time, never by hardcoded id, so a re-created
   employee row does not silently reassign somebody else's tasks.

4. Nothing here sends email. Stage and task rows live in `onboarding_*` tables;
   the ccdocs_automation send gate connects its pre_save/post_save handlers to
   `recruitment.Candidate` ONLY (`ccdocs_automation/signals.py` `_connect`), and
   its one armed path (B) fires on a recruitment-stage name containing
   "shortlist". Onboarding stages are a different table and a different model.
   Verified empirically: the 0/50 daily budget is unchanged across an apply.

ROLE STRINGS
------------
Owners are named by role, not by person, so a handover is a one-line edit here:
    FLOOR     -> the floor manager (runs the room, owns dialer + rules)
    TRAINING  -> training / QC (owns curriculum, shadowing, the ramp check)
    PAPERWORK -> contract, ID, tax form, employee record
"""

from __future__ import annotations

# --- Role -> the person who holds it today -------------------------------------
# Resolved by email at apply time. If an email resolves to no active Employee the
# apply REFUSES rather than silently leaving a task unowned.
ROLE_EMAILS = {
    "FLOOR": "joseh@ccdocs.com",  # Jose Rojas Hernandez, employee 197
    "TRAINING": "williamtorres@ccdocs.com",  # William Torres, employee 168
    "PAPERWORK": "paolap@ccdocs.com",  # Paola Palacios, employee 172
}

AGENT = "agent"
DIRECTOR = "director"

# Which track each recruitment runs. Recruitment 2 "Outbound Roofing Sales Ninja"
# is a setter posting despite the title (ccdocs_recruitment_config sets its
# role_family to 'setter'); recruitment 6 is "Roofing Appointment Setter".
# Recruitment 12 is "Call Center Manager / Director -- Rockstar Drive".
TRACK_BY_RECRUITMENT = {
    2: AGENT,
    6: AGENT,
    12: DIRECTOR,
}

# --- The skeleton --------------------------------------------------------------
# (sequence, stage_title, is_final_stage, [stage manager roles], [(task_title, [task owner roles])])
#
# Task titles are written as PROVABLE states, not activities -- "dialer login
# verified live (agent reached a call screen)" not "set up dialer". A task you
# cannot verify is a task that gets ticked without being done, which is how the
# 19-interview roster ended up with 0 rows in the ATS.

AGENT_SKELETON = [
    (
        0,
        "Day 0 -- Contract, Accounts, Credentials",
        False,
        ["PAPERWORK", "FLOOR"],
        [
            ("Contract signed AND countersigned (DocuSeal submission id recorded)", ["PAPERWORK"]),
            ("Government ID + tax form on file", ["PAPERWORK"]),
            ("Workspace account created and first sign-in completed", ["FLOOR"]),
            ("VICIdial agent seat created (user + user_group + phone)", ["FLOOR"]),
            ("Slack invite accepted as a full member (not a guest)", ["FLOOR"]),
            ("Start time confirmed in the hire's OWN timezone, in writing", ["PAPERWORK"]),
        ],
    ),
    (
        1,
        "Day 1 -- Systems, Script, Floor Rules",
        False,
        ["FLOOR"],
        [
            ("Dialer login verified live (agent reached a call screen)", ["FLOOR"]),
            ("Script + rebuttal sheet delivered and read back out loud", ["TRAINING"]),
            ("Call-center rules briefing: attendance, breaks, disposition discipline", ["FLOOR"]),
            ("QA intro: how a call is scored and what fails it", ["TRAINING"]),
        ],
    ),
    (
        2,
        "Week 1 -- Training & Certification",
        False,
        ["TRAINING"],
        [
            ("Training modules 1-3 complete (product, objections, appointment standard)", ["TRAINING"]),
            ("Live-call shadow: 2 sessions observed and debriefed", ["TRAINING"]),
            ("First 10 own dials reviewed with QA feedback recorded", ["TRAINING"]),
            ("Ramp check: 3 CAA/day hit on 2 consecutive working days", ["TRAINING", "FLOOR"]),
        ],
    ),
    (
        3,
        "Graduated -- 3 CAA/day Certified",
        True,
        ["FLOOR", "TRAINING"],
        [
            ("Graduation gate signed off: 3 CAA/day standard met", ["FLOOR", "TRAINING"]),
            ("Converted to employee in Horilla (payroll identity exists)", ["PAPERWORK"]),
        ],
    ),
]

DIRECTOR_SKELETON = [
    (
        0,
        "Day 0 -- Contract, Accounts, Credentials",
        False,
        ["PAPERWORK", "FLOOR"],
        [
            ("Contract signed AND countersigned (DocuSeal submission id recorded)", ["PAPERWORK"]),
            ("Government ID + tax form on file", ["PAPERWORK"]),
            ("Workspace account created and first sign-in completed", ["PAPERWORK"]),
            ("VICIdial MANAGER seat created (level 8 + manager user_group)", ["FLOOR"]),
            ("Slack invite accepted as a full member, added to the ops channels", ["PAPERWORK"]),
            ("Start time confirmed in the hire's OWN timezone, in writing", ["PAPERWORK"]),
        ],
    ),
    (
        1,
        "Day 1 -- Systems, Reporting, Floor Handover",
        False,
        ["FLOOR"],
        [
            ("Horilla + VICIdial reporting access verified (they can pull their own numbers)", ["FLOOR"]),
            ("Team roster handed over: who reports to them, by name", ["FLOOR"]),
            ("Call-center rules briefing: attendance, breaks, disposition discipline", ["FLOOR"]),
            ("QA calibration session with training/QC", ["TRAINING"]),
        ],
    ),
    (
        2,
        "Week 1 -- Standard, Cadence, Escalation",
        False,
        ["TRAINING"],
        [
            ("Owns the 3 CAA/agent/day standard: can state it and how it is measured", ["TRAINING"]),
            ("Coaching cadence scheduled on their own calendar", ["TRAINING"]),
            ("Escalation path documented: who they call when the floor is down", ["FLOOR"]),
        ],
    ),
    (
        3,
        "Graduated -- Running the Floor",
        True,
        ["FLOOR", "TRAINING"],
        [
            ("Graduation gate signed off: their team held 3 CAA/agent/day", ["FLOOR", "TRAINING"]),
            ("Converted to employee in Horilla (payroll identity exists)", ["PAPERWORK"]),
        ],
    ),
]

SKELETONS = {AGENT: AGENT_SKELETON, DIRECTOR: DIRECTOR_SKELETON}


def skeleton_for(recruitment_id: int, track: str | None = None):
    """Return (track, skeleton) for a recruitment. Defaults to the agent track."""
    resolved = track or TRACK_BY_RECRUITMENT.get(int(recruitment_id), AGENT)
    return resolved, SKELETONS[resolved]
