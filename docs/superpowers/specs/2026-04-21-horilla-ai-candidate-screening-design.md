# Horilla AI Candidate Screening — Design

**Date:** 2026-04-21
**Status:** Approved for implementation
**Scope:** v1

## Problem

Applications submitted via `ccdocs.com/become-a-call-center-agent/#apply` are forwarded into Horilla recruitment pipelines (e.g., the "Roofing Ninja …" pipeline) as `recruitment.Candidate` records with resume + details. Today they land ungraded — a recruiter must read each CV and answers by hand.

The companion system `job-applicant-platform` (served at `hr.ccdocs.com/hiring-portal/`) already AI-grades its own applicants via `src/lib/ai-screening.ts` — CV parsed → prompt built → DeepSeek called → `{score, summary, greenFlags[], redFlags[]}` persisted on the applicant.

We want the same grade produced for **every** `Candidate` that enters **any** Horilla pipeline, using a port of that same screening logic.

## Goals

- Every newly-created `Candidate` in Horilla is AI-screened at intake (fire-and-forget).
- Each candidate carries a persisted `ai_score` (1–10) and `ai_report` JSON with `summary`, `greenFlags`, `redFlags`.
- Score is visible as a color-coded badge on the pipeline/kanban card.
- Full report is visible on the candidate detail page.
- Existing recruitment flow stays identical — the live pipeline must not break under any failure mode in the AI path.

## Non-goals (v1)

- Porting deal-breaker flags from `job-applicant-platform.ScreeningQuestion` onto `RecruitmentSurvey`. v1 screens against resume + recruitment title/description/skills only; survey answers are skipped.
- Loom/video-URL capture for Horilla candidates (no field exists; skip from prompt).
- Auto-advance / auto-reject based on threshold. v1 only produces the score; humans still move stages.
- Re-screening on every update. v1 runs once at creation (with a manual "Re-run" admin action for explicit retries).
- Any UI or behavior change to the job-applicant-platform. It stays as-is.

## Architecture

```
ccdocs.com/become-a-call-center-agent/#apply  ── (existing forwarder) ──┐
                                                                         ▼
other intake paths (manual create, LinkedIn, bulk resume, etc.)   recruitment.Candidate.save()
                                                                         │
                                                                         ▼
                                                          post_save signal (new)
                                                                         │
                                                                         ▼
                                                     threading.Thread(daemon=True) ── fire-and-forget
                                                                         │
                                                                         ▼
                                                     recruitment.ai_screening.screen_candidate(id)
                                                     ┌───────────────────┴──────────────────┐
                                                     │ 1. load Candidate + Recruitment       │
                                                     │ 2. extract text from resume (PDF/DOCX)│
                                                     │ 3. build prompt                       │
                                                     │ 4. call DeepSeek (via Infisical key)  │
                                                     │ 5. parse JSON report                  │
                                                     │ 6. persist ai_score + ai_report       │
                                                     └───────────────────┬──────────────────┘
                                                                         ▼
                                                       kanban badge + detail "AI Screening" section
```

Infisical hydrates `os.environ` at Django startup; the screening module reads `DEEPSEEK_API_KEY` from `os.environ` the same way `job-applicant-platform`'s `callLLM` reads it from `process.env.DEEPSEEK_API_KEY`.

## Components

### 1. Schema changes — `recruitment.Candidate`

Additive only. New fields on `Candidate`:

```python
ai_score = models.FloatField(null=True, blank=True, editable=False,
                             verbose_name=_("AI Score"))
ai_report = models.JSONField(null=True, blank=True, editable=False,
                             verbose_name=_("AI Report"))
ai_screened_at = models.DateTimeField(null=True, blank=True, editable=False)
```

`ai_report` JSON shape (matches job-applicant-platform):

```json
{
  "score": 7,
  "summary": "2-3 sentence assessment",
  "greenFlags": ["...", "..."],
  "redFlags": ["...", "..."]
}
```

One Django migration. No existing columns altered. No indexes needed in v1 (we don't query on the score yet).

### 2. Infisical secrets loader — `horilla/infisical_boot.py`

Mirrors `job-applicant-platform/src/lib/infisical.ts`:

```python
# pseudocode
from infisicalsdk import InfisicalSDKClient

def load_secrets():
    client = InfisicalSDKClient(host=os.environ["INFISICAL_SITE_URL"])
    client.auth.universal_auth.login(
        client_id=os.environ["INFISICAL_CLIENT_ID"],
        client_secret=os.environ["INFISICAL_CLIENT_SECRET"],
    )
    secrets = client.secrets.list_secrets(
        project_id=os.environ["INFISICAL_PROJECT_ID"],
        environment_slug=os.environ.get("INFISICAL_ENVIRONMENT", "prod"),
        secret_path=os.environ.get("INFISICAL_SECRET_PATH", "/"),
        expand_secret_references=True,
        view_secret_value=True,
        include_imports=True,
        recursive=False,
    )
    for s in secrets:
        os.environ.setdefault(s.secret_key, s.secret_value)
```

Called once from `recruitment/apps.py::RecruitmentConfig.ready()` (or equivalent Horilla startup hook) — wrapped in try/except so Horilla boots even if Infisical is unreachable. Logs a warning on failure.

Five `INFISICAL_*` env vars live in Horilla's server `.env` (systemd EnvironmentFile). `DEEPSEEK_API_KEY` is pulled from Infisical, not stored in `.env`.

### 3. Screening module — `recruitment/ai_screening.py`

Python port of `job-applicant-platform/src/lib/ai-screening.ts`. Functions:

- `extract_cv_text(resume_field) -> str`
  - `.pdf` → `pdfplumber` (fallback to `pypdf` if plumber raises)
  - `.doc` / `.docx` → `python-docx` (fallback to raw text extract)
  - Unknown / corrupt → returns a placeholder string the model can handle, same as TS version
  - Truncates at 8 000 chars (match TS)
- `build_prompt(candidate, cv_text, recruitment) -> str`
  - Ports the TS template verbatim, minus deal-breakers and Loom sections
  - Role description = `recruitment.title + "\n" + (recruitment.description or "")`
  - Requirements = list of `recruitment.skills.all()` names, numbered
  - Applicant fields: name, email, portfolio, schedule_date (mapped to "Available From"), today's date
- `call_deepseek(prompt) -> str`
  - Uses the `openai` SDK with `base_url="https://api.deepseek.com"` and `model="deepseek-reasoner"`, identical to TS version
  - `api_key = os.environ["DEEPSEEK_API_KEY"]`
- `parse_report(raw) -> dict`
  - Tries `json.loads`, falls back to regex `\{[\s\S]*\}` match, final fallback is the same error stub as TS
- `screen_candidate(candidate_id: int) -> None`
  - Loads candidate + recruitment
  - Calls the above, persists `ai_score`, `ai_report`, `ai_screened_at` via `Candidate.objects.filter(id=...).update(...)` (avoids re-triggering the post_save signal)

### 4. Trigger — post_save signal

New file `recruitment/signals_ai_screening.py`:

```python
@receiver(post_save, sender=Candidate)
def trigger_ai_screening(sender, instance, created, **kwargs):
    if not created:
        return
    if not instance.resume:
        return
    def _run():
        try:
            screen_candidate(instance.id)
        except Exception:
            logger.exception("AI screening failed for candidate %s", instance.id)
    threading.Thread(target=_run, daemon=True).start()
```

Connected in `recruitment/apps.py::ready()`. All exceptions swallowed inside the thread — the outer `Candidate.save()` returns cleanly no matter what happens in the thread.

### 5. UI — kanban badge

In the kanban candidate-card template (`recruitment/templates/pipeline/kanban_components/candidate_kanban_components.html` is the likely touchpoint — verified during implementation), render a small chip:

- `ai_score is None` → "—" (neutral gray) with tooltip "Not yet screened"
- `ai_score <= 3` → red chip "AI: {score}/10"
- `ai_score <= 6` → yellow chip "AI: {score}/10"
- `ai_score >= 7` → green chip "AI: {score}/10"

Chip is a single additive `<span>` with Horilla's existing badge CSS classes (oh-badge or similar — follow local conventions). Clicking it jumps to the detail section below.

### 6. UI — candidate detail "AI Screening" section

New partial `recruitment/templates/candidate/ai_screening_section.html`, included on the candidate detail page. Renders:

- Big score chip (same color logic as kanban)
- "Summary" block (plain text)
- "Green flags" — green bullet list
- "Red flags" — red bullet list
- "Screened at {ai_screened_at}" footer
- "Re-run AI screening" button (POSTs to a new view that calls `screen_candidate` inline with a permission check)

If `ai_report is None`, render "AI screening has not completed for this candidate yet." + the re-run button.

### 7. Re-run endpoint

Single new view + URL:

- `POST /recruitment/candidate/<id>/ai-screen/rerun/`
- Permission: recruitment manager or superuser
- Calls `screen_candidate(id)` inline (not threaded — user is waiting), returns HTMX fragment replacing the AI Screening section

## Error handling / safety invariants

| Failure mode | Behavior |
|---|---|
| Infisical unreachable at startup | Django still boots. Screening module logs a warning each call and writes `ai_report = {"error": "DEEPSEEK_API_KEY not configured"}` with `score=0`. |
| `DEEPSEEK_API_KEY` missing | Screening short-circuits with the same error report. Candidate still created. |
| DeepSeek API error / timeout | Exception caught in thread. Candidate still created. `ai_score` stays `None`. Recruiter sees "not yet screened" and can re-run. |
| PDF corrupt / unsupported file | `extract_cv_text` returns a placeholder string; LLM still scored with reduced info. Mirrors TS behavior. |
| LLM returns non-JSON | `parse_report` fallback produces `{score: 0, summary: "manual review required", ...}`. |
| `Candidate.save()` raises | Signal never fires; no screening attempted; existing behavior. |
| `post_save` signal itself raises | Wrapped in try/except inside `_run`. Candidate creation path never sees it. |

**Hard rule:** any failure in the AI path results in `ai_score=None` (or error-stub report). It NEVER prevents the candidate from being created, visible in kanban, or moved through stages.

## Dependencies added

- `infisicalsdk` (Python SDK, PyPI) — for secret loading
- `openai` (already in Horilla? verify; used with DeepSeek base_url)
- `pdfplumber` — PDF text extraction
- `python-docx` — DOCX extraction

All added to `requirements.txt`. Verify no version conflicts with existing Horilla deps during implementation.

## Rollout plan

1. **Local dev**
   - Apply migration, confirm no existing tests break.
   - Create a test candidate via the admin — verify normal flow (card appears in kanban, stages move). AI badge renders "—".
   - Set `DEEPSEEK_API_KEY` locally, re-submit — verify score and report persist, badge colors correctly.
2. **Staging**
   - Deploy code + migration.
   - Confirm Infisical hydrates `DEEPSEEK_API_KEY` on boot.
   - Submit a real resume via the ccdocs.com form (or simulate the forwarded POST) — verify candidate lands in the staging "Roofing Ninja" (or equivalent) pipeline AND receives an AI score within ~30s.
   - Kill network to `api.deepseek.com` and submit another — verify candidate still created with `ai_score=None` and no 500 on the intake path.
3. **Prod**
   - Run migration.
   - Deploy code.
   - Smoke-test by creating one candidate manually and confirming both the existing pipeline behavior and the new AI score.
   - Watch logs for the first 10 real candidates; confirm scores are sane and no intake failures.

## Files touched

New:
- `horilla/infisical_boot.py`
- `recruitment/ai_screening.py`
- `recruitment/signals_ai_screening.py`
- `recruitment/templates/candidate/ai_screening_section.html`
- `recruitment/migrations/XXXX_candidate_ai_fields.py` (generated)
- `docs/superpowers/specs/2026-04-21-horilla-ai-candidate-screening-design.md` (this file)

Modified (additive only):
- `recruitment/models.py` — add three fields to `Candidate`
- `recruitment/apps.py` — wire `ready()` to call `infisical_boot.load_secrets()` and import the signal module
- `recruitment/urls.py` — add re-run endpoint URL
- `recruitment/views.py` (or a new views module) — add re-run view
- `recruitment/templates/pipeline/kanban_components/candidate_kanban_components.html` — append badge
- `recruitment/templates/candidate/candidate_view.html` (or wherever the detail template lives — verify during impl) — include `ai_screening_section.html`
- `requirements.txt` — add new deps

## Out of scope (followups tracked but not built)

- Deal-breaker flags on `RecruitmentSurvey` + survey answers in the prompt
- Auto-advance/auto-reject thresholds per recruitment
- Re-screen on resume replacement
- Batch backfill of AI scores for existing candidates
- Dashboard aggregate metrics (avg score, distribution)
