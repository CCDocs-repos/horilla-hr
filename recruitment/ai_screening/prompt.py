"""Build the LLM prompt for candidate screening.

Ported from /opt/workspace/job-applicant-platform/src/lib/ai-screening.ts
buildPrompt(), minus deal-breakers and Loom video (not in Horilla's schema).
"""

from datetime import date

MAX_CV_CHARS = 8000


def _cv_truncate(text: str) -> str:
    return text[:MAX_CV_CHARS]


def build_prompt(candidate, cv_text: str, recruitment) -> str:
    title = (getattr(recruitment, "title", None) or "Open Position").strip()
    description = (getattr(recruitment, "description", None) or "").strip()
    role_description = title if not description else f"{title} — {description}"

    skills = []
    try:
        for sk in recruitment.skills.all():
            skills.append(str(sk))
    except Exception:
        pass
    requirements_list = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(skills))

    today = date.today().isoformat()
    portfolio = getattr(candidate, "portfolio", "") or "Not provided"
    available_from = getattr(candidate, "schedule_date", None)
    available_from_str = available_from.isoformat() if available_from else "Not provided"

    return f"""You are an expert HR screening assistant. Evaluate this job applicant for a {role_description}.
Important: Today's date is {today}. Evaluate the candidate's availability date relative to today.

Key requirements:
{requirements_list}

APPLICANT:
Name: {candidate.name}
Email: {candidate.email}
Portfolio: {portfolio}
Available From: {available_from_str}
Today's Date: {today}

CV CONTENT:
{_cv_truncate(cv_text)}

Return ONLY a valid JSON object -- no markdown, no explanation, just raw JSON:
{{
  "score": <1-10>,
  "summary": "<2-3 sentence assessment>",
  "greenFlags": ["<positive point>"],
  "redFlags": ["<concern or gap>"]
}}"""
