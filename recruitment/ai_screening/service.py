"""Orchestrate a single candidate screening pass.

Loads the candidate + its recruitment, extracts CV text, builds the prompt,
calls DeepSeek, parses the report, and persists ai_score / ai_report /
ai_screened_at via queryset.update() so the Candidate post_save signal is
not re-triggered.

All exceptions are caught and logged — callers (the signal handler, the
re-run view) rely on this function to never raise.
"""

import json
import logging
from django.utils import timezone

from recruitment.ai_screening.cv_text import extract_cv_text
from recruitment.ai_screening.llm import call_llm, parse_report
from recruitment.ai_screening.prompt import build_prompt
from recruitment.models import Candidate

logger = logging.getLogger(__name__)


def screen_candidate(candidate_id: int) -> None:
    try:
        candidate = Candidate.objects.select_related("recruitment_id").filter(
            id=candidate_id
        ).first()
        if candidate is None:
            logger.warning("screen_candidate: no candidate with id=%s", candidate_id)
            return
        if not candidate.resume:
            logger.info("screen_candidate: candidate %s has no resume; skipping", candidate_id)
            return
        if not candidate.recruitment_id:
            logger.info("screen_candidate: candidate %s has no recruitment; skipping", candidate_id)
            return

        cv_text = extract_cv_text(candidate.resume)
        prompt = build_prompt(candidate, cv_text, candidate.recruitment_id)
        raw = call_llm(prompt)
        report = parse_report(raw)

        score = report.get("score")
        try:
            score_float = float(score) if score is not None else None
        except (TypeError, ValueError):
            score_float = None

        Candidate.objects.filter(id=candidate_id).update(
            ai_score=score_float,
            ai_report=report,
            ai_screened_at=timezone.now(),
        )
        logger.info("screen_candidate: persisted score=%s for candidate=%s", score_float, candidate_id)
    except Exception:
        logger.exception("screen_candidate: failed for candidate=%s", candidate_id)
