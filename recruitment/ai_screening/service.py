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


def _application_text_fallback(candidate) -> str:
    """No CV uploaded -- assemble the evaluation text from the application
    itself (contact fields + screening-survey answers). Website applicants
    (ccdocs.com/sdr and siblings) never upload a CV, and skipping them meant
    the web funnel was never auto-scored (found 2026-07-18)."""
    lines = [
        "(No resume/CV was uploaded -- this is a website application. "
        "Evaluate from the application details and screening answers below.)",
        f"Phone: {candidate.mobile or 'Not provided'}",
    ]
    if candidate.country:
        lines.append(f"Country: {candidate.country}")
    try:
        from recruitment.models import RecruitmentSurveyAnswer

        for ans in RecruitmentSurveyAnswer.objects.filter(candidate_id=candidate):
            aj = ans.answer_json or {}
            if isinstance(aj, dict):
                for q, a in aj.items():
                    if a:
                        lines.append(f"Q: {q}\nA: {a}")
    except Exception:
        logger.exception(
            "screen_candidate: could not read survey answers for %s", candidate.id
        )
    return "\n\n".join(lines)


def screen_candidate(candidate_id: int) -> None:
    try:
        candidate = Candidate.objects.select_related("recruitment_id").filter(
            id=candidate_id
        ).first()
        if candidate is None:
            logger.warning("screen_candidate: no candidate with id=%s", candidate_id)
            return
        if not candidate.recruitment_id:
            logger.info("screen_candidate: candidate %s has no recruitment; skipping", candidate_id)
            return

        if not candidate.resume:
            logger.info(
                "screen_candidate: candidate %s has no resume; screening from application data",
                candidate_id,
            )
            cv_text = _application_text_fallback(candidate)
        else:
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
