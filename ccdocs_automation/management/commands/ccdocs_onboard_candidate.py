"""Django management command: put a hired candidate into onboarding, and
optionally convert them to an employee -- without raw SQL, and without sending.

    # plan for one person
    docker exec horilla-server python manage.py ccdocs_onboard_candidate -c 1400

    # do it
    docker exec horilla-server python manage.py ccdocs_onboard_candidate -c 1400 --apply

    # also create the employee/payroll identity
    docker exec horilla-server python manage.py ccdocs_onboard_candidate -c 1400 --convert --apply

    # the Tuesday class: every hired candidate on a req, metered
    docker exec horilla-server python manage.py ccdocs_onboard_candidate -r 2 --all-hired --limit 35 --apply

WHY THIS EXISTS WHEN HORILLA ALREADY HAS A CONVERT BUTTON
---------------------------------------------------------
It does: `recruitment/views/views.py::candidate_conversion`. We deliberately
reuse its exact field mapping so both paths produce identical employee rows.
But measured against the live data on 2026-08-17 it cannot carry this class:

  1. IT CRASHES FOR 113 OF THE 754 CANDIDATES ON RECRUITMENTS 2/6/12.
     Line 1712 reads `candidate_obj.job_position_id.department_id`, and 113 of
     those candidates have `job_position_id IS NULL` -> AttributeError:
     'NoneType' object has no attribute 'department_id'. They are NULL because
     they were inserted by the raw-SQL intake path, which skips
     `Candidate.save()` -- and `Candidate.save()` is the thing that backfills
     job_position from the recruitment. So the bypass at intake becomes a crash
     at conversion. Verified by evaluating the exact expression over all 754.
     This command backfills job_position from the recruitment first, which is
     precisely what `Candidate.save()` would have done.

  2. IT DOES NOT ENTER ONBOARDING. It sets `converted_employee_id` and stops.
     No `CandidateStage`, no `CandidateTask`, no `start_onboard`. So a converted
     hire never appears on the onboarding board and no task is ever owned. This
     is why `onboarding_candidatestage` = 0 while one candidate is converted.

  3. It is a click. Thirty-five clicks is not a Tuesday plan.

WHAT WE DELIBERATELY DO NOT DO
------------------------------
* We do NOT set `candidate.converted = True`. Read `Candidate.save()`:
      if self.converted:
          self.hired = False
  Setting it ERASES the hire. THE SCOREBOARD IS HIRES, so a correctly-converted
  class of 30 would make `count(*) WHERE hired` read 0. Horilla's own convert
  button does not set it either, so leaving it False keeps the two paths
  identical. Count hires as `hired OR converted_employee_id IS NOT NULL`.
  (Candidate 787 Pedro Orozco has hired=t AND converted=t, a combination
  `save()` cannot produce -- that row was written by raw SQL.)

* We do NOT create an `auth_user` login unless asked (`--create-login`).
  Account provisioning belongs to hr-access, not here.

* We NEVER touch `stage_id` or `canceled`. Those are the only two fields the
  ccdocs_automation send router reacts to (`signals.py::_handle_updated`), and
  its one armed path (B) fires on a recruitment-stage name containing
  "shortlist". Not touching them is what makes this command silent. The gate's
  daily counter is printed before and after so the claim is measured.

* Candidate 1325 (Jose Rojas Hernandez) is HARD REFUSED. He is being converted
  by hand in a separate lane; two writers on one row is how you get the
  half-converted state this command exists to prevent.

Exit codes:
    0 = converged (or a clean plan)
    1 = refused on a precondition. Nothing written.
    2 = could not run (import/DB failure).
"""

from __future__ import annotations

import json
import sys

from django.core.management.base import BaseCommand
from django.db import transaction

# Owned by another lane. See module docstring.
LANE_RESERVED_CANDIDATE_IDS = {1325}


class Refused(Exception):
    """A precondition failed. Nothing has been written."""


class Command(BaseCommand):
    help = "Enter a hired candidate into onboarding; optionally convert to employee."

    def add_arguments(self, parser):
        parser.add_argument("-c", "--candidate", type=int, action="append", default=None)
        parser.add_argument("-r", "--recruitment", type=int, default=None)
        parser.add_argument(
            "--all-hired",
            action="store_true",
            help="Every hired, not-yet-onboarded candidate on --recruitment.",
        )
        parser.add_argument("--limit", type=int, default=40, help="Batch ceiling.")
        parser.add_argument(
            "--convert",
            action="store_true",
            help="Also create the Employee + work-info (payroll identity).",
        )
        parser.add_argument(
            "--create-login",
            action="store_true",
            help="With --convert, also create an auth_user (no password mail).",
        )
        parser.add_argument("--apply", action="store_true", help="Write the changes.")
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **options):
        try:
            from django.contrib.auth.models import User
            from employee.models import Employee
            from onboarding.models import CandidateStage, CandidateTask, OnboardingStage, OnboardingTask
            from recruitment.models import Candidate
        except Exception as exc:  # pragma: no cover
            self.stderr.write(f"could not load models: {exc!r}")
            sys.exit(2)

        apply = options["apply"]
        as_json = options["json"]
        budget_before = _gate_counter()
        report = []

        try:
            candidates = self._select(options, Candidate)

            with transaction.atomic():
                for cand in candidates:
                    entry = {"candidate": cand.id, "name": cand.name, "steps": []}

                    def step(name, detail=""):
                        entry["steps"].append(
                            {"step": name, "detail": detail, "applied": apply}
                        )

                    # --- 0. job_position backfill: the fix for the native crash ---
                    if cand.job_position_id is None:
                        jp = cand.recruitment_id.job_position_id
                        if jp is None:
                            raise Refused(
                                f"candidate {cand.id}: job_position is NULL and "
                                f"recruitment {cand.recruitment_id_id} has no default "
                                f"job_position to backfill from."
                            )
                        step("backfill_job_position", f"-> {jp.id} {jp.job_position}")
                        if apply:
                            cand.job_position_id = jp

                    # --- 1. the onboarding stage skeleton must exist -------------
                    stages = list(
                        OnboardingStage.objects.filter(
                            recruitment_id=cand.recruitment_id
                        ).order_by("sequence")
                    )
                    if not stages:
                        raise Refused(
                            f"recruitment {cand.recruitment_id_id} has no onboarding "
                            f"stages. Run: manage.py ccdocs_onboarding_skeleton "
                            f"-r {cand.recruitment_id_id} --apply"
                        )
                    entry_stage = stages[0]

                    # --- 2. optional employee conversion ------------------------
                    if options["convert"]:
                        emp = cand.converted_employee_id or Employee.objects.filter(
                            email=cand.email
                        ).first()
                        if emp is not None:
                            step("employee_exists", f"employee {emp.id}")
                        else:
                            step("create_employee", cand.email)
                            if apply:
                                # Field-for-field the same as
                                # recruitment/views/views.py::candidate_conversion
                                emp = Employee(
                                    employee_first_name=cand.name,
                                    email=cand.email,
                                    phone=cand.mobile,
                                    gender=cand.gender,
                                    is_directly_converted=True,
                                )
                                emp.save()
                                work = emp.employee_work_info
                                work.job_position_id = cand.job_position_id
                                work.department_id = cand.job_position_id.department_id
                                work.company_id = cand.recruitment_id.company_id
                                work.email = cand.email
                                work.date_joining = cand.joining_date
                                work.save()

                        if options["create_login"] and apply and emp is not None:
                            if not User.objects.filter(username=cand.email).exists():
                                user = User(username=cand.email, email=cand.email)
                                user.set_unusable_password()
                                user.save()
                                emp.employee_user_id = user
                                emp.save()
                                step("create_login", cand.email)

                        if apply and emp is not None and cand.converted_employee_id_id != emp.id:
                            cand.converted_employee_id = emp
                        if not apply:
                            step("link_converted_employee", "candidate.converted_employee_id")

                    # --- 3. enter onboarding ------------------------------------
                    step("start_onboard", "True")
                    if apply:
                        cand.start_onboard = True
                        # NOTE: stage_id and canceled are untouched on purpose.
                        cand.save()

                    cstage = CandidateStage.objects.filter(candidate_id=cand).first()
                    if cstage is None:
                        step("create_candidate_stage", entry_stage.stage_title)
                        if apply:
                            cstage = CandidateStage(
                                candidate_id=cand, onboarding_stage_id=entry_stage
                            )
                            cstage.save()
                    else:
                        step(
                            "candidate_stage_exists",
                            cstage.onboarding_stage_id.stage_title,
                        )

                    # --- 4. materialize every task on the req --------------------
                    tasks = OnboardingTask.objects.filter(stage_id__in=stages)
                    if not tasks.exists():
                        raise Refused(
                            f"recruitment {cand.recruitment_id_id} has stages but no "
                            f"onboarding tasks. Run ccdocs_onboarding_skeleton --apply."
                        )
                    made = 0
                    for task in tasks:
                        exists = CandidateTask.objects.filter(
                            candidate_id=cand, onboarding_task_id=task
                        ).exists()
                        if not exists:
                            made += 1
                            if apply:
                                CandidateTask(
                                    candidate_id=cand,
                                    stage_id=task.stage_id,
                                    onboarding_task_id=task,
                                    status="todo",
                                ).save()
                                # Horilla's own task_creation also puts the
                                # candidate on the task's M2M; the board reads it.
                                task.candidates.add(cand)
                    step("materialize_tasks", f"{made} new of {tasks.count()}")

                    report.append(entry)

                if not apply:
                    transaction.set_rollback(True)

        except Refused as exc:
            payload = {"ok": False, "refused": str(exc)}
            self.stderr.write(json.dumps(payload) if as_json else f"REFUSED: {exc}")
            sys.exit(1)
        except Exception as exc:  # pragma: no cover
            self.stderr.write(f"FAILED: {type(exc).__name__}: {exc}")
            sys.exit(2)

        budget_after = _gate_counter()
        result = {
            "ok": True,
            "applied": apply,
            "candidates": len(report),
            "convert": options["convert"],
            "gate_sends_before": budget_before,
            "gate_sends_after": budget_after,
            "gate_clean": budget_before == budget_after,
            "report": report,
        }

        if as_json:
            self.stdout.write(json.dumps(result, indent=2))
            return

        mode = "APPLIED" if apply else "DRY RUN (nothing written)"
        self.stdout.write(f"=== onboard candidate -- {mode} ===")
        for entry in report:
            self.stdout.write(f"\ncandidate {entry['candidate']}  {entry['name']}")
            for s in entry["steps"]:
                self.stdout.write(f"    {s['step']:<26} {s['detail']}")
        self.stdout.write(
            f"\n{len(report)} candidate(s).  gate daily sends: "
            f"before={budget_before} after={budget_after} "
            f"{'CLEAN' if budget_before == budget_after else 'CHANGED -- INVESTIGATE'}"
        )

    def _select(self, options, Candidate):
        ids = options["candidate"] or []
        reserved = set(ids) & LANE_RESERVED_CANDIDATE_IDS
        if reserved:
            raise Refused(
                f"candidate(s) {sorted(reserved)} are reserved to another lane and "
                f"are being converted by hand there. Refusing to be a second writer."
            )

        if options["all_hired"]:
            if not options["recruitment"]:
                raise Refused("--all-hired requires -r/--recruitment")
            qs = (
                Candidate.objects.filter(
                    recruitment_id_id=options["recruitment"],
                    hired=True,
                    is_active=True,
                    canceled=False,
                )
                .exclude(id__in=LANE_RESERVED_CANDIDATE_IDS)
                .exclude(onboarding_stage__isnull=False)
                .order_by("id")[: options["limit"]]
            )
            found = list(qs)
            if not found:
                raise Refused(
                    f"recruitment {options['recruitment']} has no hired, active, "
                    f"not-yet-onboarded candidates."
                )
            return found

        if not ids:
            raise Refused("give -c/--candidate <id> (repeatable) or -r <id> --all-hired")

        found = list(Candidate.objects.filter(id__in=ids))
        missing = set(ids) - {c.id for c in found}
        if missing:
            raise Refused(f"candidate(s) {sorted(missing)} do not exist")
        not_hired = [c.id for c in found if not c.hired]
        if not_hired:
            raise Refused(
                f"candidate(s) {not_hired} are not hired. Onboarding a non-hire is "
                f"how you onboard the wrong person; move them to a hired stage first."
            )
        return found


def _gate_counter():
    """Today's real-send count from the ccdocs_automation ledger, or None."""
    try:
        from ccdocs_automation.send_ledger import day_total_real_sends

        return int(day_total_real_sends())
    except Exception:
        return None
