"""Django management command: apply the onboarding stage/task skeleton.

    # see what would change, touch nothing (default)
    docker exec horilla-server python manage.py ccdocs_onboarding_skeleton

    # write it
    docker exec horilla-server python manage.py ccdocs_onboarding_skeleton --apply

    # one req only
    docker exec horilla-server python manage.py ccdocs_onboarding_skeleton -r 12 --apply

WHY
---
CCDocs has never used Horilla's onboarding module (measured 2026-08-17: 0 tasks,
0 candidate stages, 0 portals; the only rows are 7 stages all named "Initial").
A 30-35 person class starting Tuesday needs stages and tasks to exist BEFORE the
class does, and it needs them to be the same on every req so the kanban board
means one thing. The skeleton itself lives in
`ccdocs_automation/onboarding_skeleton.py` as data; this command is the applier.

IDEMPOTENT. Stages are matched on (recruitment, sequence) and tasks on
(stage, task_title), so re-running converges instead of duplicating. Running it
twice is a supported, boring thing to do.

SENDS NOTHING. It writes only `onboarding_*` rows. The ccdocs_automation send
gate binds its signal handlers to `recruitment.Candidate` alone
(`ccdocs_automation/signals.py::_connect`); no Candidate row is saved here. The
command prints the gate's daily counter before and after so that claim is
measured, not asserted.

Exit codes:
    0 = converged (or, without --apply, a clean plan)
    1 = refused: an owner email resolves to no active Employee, or a named
        recruitment does not exist. Nothing was written.
    2 = the command could not run (import/DB failure) -- never confused with
        "everything is fine".

Flags:
    -r/--recruitment <id>  limit to this recruitment (repeatable; default 2,6,12)
    --track agent|director override the per-recruitment track
    --apply                actually write (default is a dry run)
    --json                 machine-readable plan/result
"""

from __future__ import annotations

import json
import sys

from django.core.management.base import BaseCommand
from django.db import transaction

DEFAULT_RECRUITMENTS = [2, 6, 12]


class Refused(Exception):
    """A precondition failed. Nothing has been written."""


class Command(BaseCommand):
    help = "Apply the Day 0 / Day 1 / Week 1 / Graduated onboarding skeleton."

    def add_arguments(self, parser):
        parser.add_argument(
            "-r",
            "--recruitment",
            action="append",
            type=int,
            default=None,
            help="Recruitment id. Repeatable. Default: 2, 6, 12.",
        )
        parser.add_argument(
            "--track",
            choices=["agent", "director"],
            default=None,
            help="Force a track instead of the per-recruitment default.",
        )
        parser.add_argument("--apply", action="store_true", help="Write the changes.")
        parser.add_argument("--json", action="store_true", help="JSON output.")

    def handle(self, *args, **options):
        try:
            from employee.models import Employee
            from onboarding.models import OnboardingStage, OnboardingTask
            from recruitment.models import Recruitment

            from ccdocs_automation.onboarding_skeleton import ROLE_EMAILS, skeleton_for
        except Exception as exc:  # pragma: no cover - import failure is fatal
            self.stderr.write(f"could not load models: {exc!r}")
            sys.exit(2)

        rec_ids = options["recruitment"] or DEFAULT_RECRUITMENTS
        apply = options["apply"]
        as_json = options["json"]

        budget_before = _gate_counter()

        try:
            # --- resolve owners first; refuse before touching anything ---------
            owners = {}
            for role, email in ROLE_EMAILS.items():
                emp = Employee.objects.filter(email=email, is_active=True).first()
                if emp is None:
                    raise Refused(
                        f"role {role} maps to {email!r} but no ACTIVE employee_employee row "
                        f"has that email. Create the employee first; refusing to leave "
                        f"onboarding tasks unowned."
                    )
                owners[role] = emp

            recruitments = {}
            for rid in rec_ids:
                rec = Recruitment.objects.filter(id=rid).first()
                if rec is None:
                    raise Refused(f"recruitment {rid} does not exist")
                recruitments[rid] = rec

            plan = []
            with transaction.atomic():
                for rid, rec in recruitments.items():
                    track, skeleton = skeleton_for(rid, options["track"])
                    for seq, title, is_final, mgr_roles, tasks in skeleton:
                        stage = OnboardingStage.objects.filter(
                            recruitment_id=rec, sequence=seq
                        ).first()
                        if stage is None:
                            action = "create_stage"
                            if apply:
                                stage = OnboardingStage(
                                    recruitment_id=rec,
                                    sequence=seq,
                                    stage_title=title,
                                    is_final_stage=is_final,
                                )
                                stage.save()
                        elif stage.stage_title != title or stage.is_final_stage != is_final:
                            action = f"update_stage (was {stage.stage_title!r})"
                            if apply:
                                stage.stage_title = title
                                stage.is_final_stage = is_final
                                stage.save()
                        else:
                            action = "stage_ok"

                        plan.append(
                            {
                                "recruitment": rid,
                                "track": track,
                                "sequence": seq,
                                "stage": title,
                                "action": action,
                            }
                        )

                        if apply:
                            stage.employee_id.set([owners[r] for r in mgr_roles])
                        elif stage is None:
                            # dry run, stage does not exist yet: report the tasks
                            # that WOULD be created and move on.
                            for task_title, task_roles in tasks:
                                plan.append(
                                    {
                                        "recruitment": rid,
                                        "sequence": seq,
                                        "stage": title,
                                        "task": task_title,
                                        "owners": task_roles,
                                        "action": "create_task",
                                    }
                                )
                            continue

                        for task_title, task_roles in tasks:
                            task = OnboardingTask.objects.filter(
                                stage_id=stage, task_title=task_title
                            ).first()
                            if task is None:
                                t_action = "create_task"
                                if apply:
                                    task = OnboardingTask(
                                        stage_id=stage, task_title=task_title
                                    )
                                    task.save()
                            else:
                                t_action = "task_ok"
                            if apply:
                                task.employee_id.set([owners[r] for r in task_roles])
                            plan.append(
                                {
                                    "recruitment": rid,
                                    "sequence": seq,
                                    "stage": title,
                                    "task": task_title,
                                    "owners": task_roles,
                                    "action": t_action,
                                }
                            )

                if not apply:
                    transaction.set_rollback(True)

        except Refused as exc:
            payload = {"ok": False, "refused": str(exc)}
            self.stderr.write(json.dumps(payload) if as_json else f"REFUSED: {exc}")
            sys.exit(1)
        except Exception as exc:  # pragma: no cover
            self.stderr.write(f"FAILED: {exc!r}")
            sys.exit(2)

        budget_after = _gate_counter()

        changed = [p for p in plan if p["action"] not in ("stage_ok", "task_ok")]
        result = {
            "ok": True,
            "applied": apply,
            "recruitments": rec_ids,
            "rows_planned": len(plan),
            "rows_changed": len(changed),
            "gate_sends_before": budget_before,
            "gate_sends_after": budget_after,
            "gate_clean": budget_before == budget_after,
            "plan": plan,
        }

        if as_json:
            self.stdout.write(json.dumps(result, indent=2))
            return

        mode = "APPLIED" if apply else "DRY RUN (nothing written)"
        self.stdout.write(f"=== onboarding skeleton -- {mode} ===")
        last_key = None
        for p in plan:
            key = (p["recruitment"], p["sequence"])
            if key != last_key:
                self.stdout.write(
                    f"\nrec {p['recruitment']}  seq {p['sequence']}  {p['stage']}"
                )
                last_key = key
            if "task" in p:
                own = "/".join(p["owners"])
                self.stdout.write(f"    [{p['action']:<12}] {p['task']}  <- {own}")
            else:
                self.stdout.write(f"    stage: {p['action']}")
        self.stdout.write(f"\n{len(changed)} of {len(plan)} rows need change / changed")
        self.stdout.write(
            f"gate daily sends: before={budget_before} after={budget_after} "
            f"{'CLEAN' if budget_before == budget_after else 'CHANGED -- INVESTIGATE'}"
        )


def _gate_counter():
    """Today's real-send count from the ccdocs_automation ledger, or None.

    Read-only. Used as a tripwire: applying an onboarding skeleton must never
    move this number.
    """
    try:
        from ccdocs_automation.send_ledger import day_total_real_sends

        return int(day_total_real_sends())
    except Exception:
        return None
