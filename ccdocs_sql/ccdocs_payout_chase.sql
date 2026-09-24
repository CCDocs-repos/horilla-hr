-- CCDocs: payout chase table.
--
-- One row per "we are chasing this person for missing payment details" job.
-- Written by the Chase for details button on the employee profile
-- (employee/views.py :: payout_chase_start / payout_chase_stop).
-- Read and advanced by an outside chaser job, which owns ALL scheduling once
-- the row exists -- Horilla only opens and closes it.
--
-- The ids inside `fields` come from ccdocs_payout_fields.json. Never rename an
-- id: open rows hold it.
--
-- Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS, so re-running is safe.
CREATE TABLE IF NOT EXISTS ccdocs_payout_chase (
    id              BIGSERIAL PRIMARY KEY,
    employee_id     BIGINT NOT NULL
                        REFERENCES employee_employee(id) ON DELETE CASCADE,
    fields          JSONB NOT NULL,              -- JSON array of field ids from ccdocs_payout_fields.json
    opened_by_id    INTEGER
                        REFERENCES auth_user(id) ON DELETE SET NULL,
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    next_touch_at   TIMESTAMPTZ,                 -- when the chaser should next send
    touch_no        INTEGER NOT NULL DEFAULT 0,  -- how many touches sent so far
    last_touch_at   TIMESTAMPTZ,
    last_outcome    TEXT,                        -- short human text, e.g. 'call, no answer'
    resolved_at     TIMESTAMPTZ,
    resolved_how    TEXT,
    stopped_by_id   INTEGER
                        REFERENCES auth_user(id) ON DELETE SET NULL,
    CONSTRAINT ccdocs_payout_chase_resolved_how_check CHECK (
        resolved_how IS NULL
        -- 'gave_up' is the chaser hitting its touch cap and handing the person
        -- to a human instead of chasing them forever.
        OR resolved_how IN ('filled', 'replied_done', 'stopped', 'offboarded', 'gave_up')
    )
);

-- How the chaser finds the work that is due.
CREATE INDEX IF NOT EXISTS ccdocs_payout_chase_due_idx
    ON ccdocs_payout_chase (resolved_at, next_touch_at);

-- ONE OPEN CHASE PER EMPLOYEE. A person can be chased again after the last chase
-- was closed, but never twice at once.
CREATE UNIQUE INDEX IF NOT EXISTS ccdocs_payout_chase_one_open_idx
    ON ccdocs_payout_chase (employee_id)
    WHERE resolved_at IS NULL;

COMMENT ON TABLE ccdocs_payout_chase IS
    'CCDocs: chases for an employee''s missing payment details';
