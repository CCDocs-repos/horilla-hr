import logging
import threading

from django.db import transaction
from django.db.models.signals import m2m_changed, post_save
from django.dispatch import receiver

from recruitment.models import (
    Candidate,
    CandidateDocument,
    CandidateDocumentRequest,
    Recruitment,
    Stage,
)


from recruitment.ai_screening.service import screen_candidate

_ai_logger = logging.getLogger(__name__)


@receiver(post_save, sender=Candidate)
def trigger_ai_screening(sender, instance, created, **kwargs):
    """Fire-and-forget AI screening on Candidate creation.

    Must never raise — a failure here must not prevent Candidate.save() from
    completing or the caller's HTTP request from succeeding.
    """
    if not created:
        return
    # No-resume candidates (website applicants) are screened too -- the
    # service falls back to application text + screening answers (2026-07-18).
    candidate_id = instance.id

    def _run():
        try:
            screen_candidate(candidate_id)
        except Exception:
            _ai_logger.exception("AI screening thread failed for candidate=%s", candidate_id)

    try:
        # Spawn only after the creating transaction commits -- a thread started
        # inside the ingest view's transaction races it and finds no candidate
        # (seen live 2026-07-18: "no candidate with id=838"). on_commit runs
        # immediately when there is no active transaction.
        transaction.on_commit(
            lambda: threading.Thread(target=_run, daemon=True).start()
        )
    except Exception:
        _ai_logger.exception("Failed to spawn AI screening thread for candidate=%s", candidate_id)


@receiver(post_save, sender=Recruitment)
def create_initial_stage(sender, instance, created, **kwargs):
    """
    This is post save method, used to create initial stage for the recruitment
    """
    if created:
        applied_stage = Stage()
        applied_stage.sequence = 0
        applied_stage.recruitment_id = instance
        applied_stage.stage = "Applied"
        applied_stage.stage_type = "applied"
        applied_stage.save()

        initial_stage = Stage()
        initial_stage.sequence = 1
        initial_stage.recruitment_id = instance
        initial_stage.stage = "Initial"
        initial_stage.stage_type = "initial"
        initial_stage.save()


@receiver(m2m_changed, sender=CandidateDocumentRequest.candidate_id.through)
def document_request_m2m_changed(sender, instance, action, **kwargs):
    if action == "post_add":
        candidate_document_create(instance)

    elif action == "post_remove":
        candidate_document_create(instance)


def candidate_document_create(instance):
    candidates = instance.candidate_id.all()
    for candidate in candidates:
        document, created = CandidateDocument.objects.get_or_create(
            candidate_id=candidate,
            document_request_id=instance,
            defaults={"title": f"Upload {instance.title}"},
        )
        document.title = f"Upload {instance.title}"
        document.save()
