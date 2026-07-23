-- app.sheets.add_job_to_beacon deliberately only extracts salary from the
-- stored (possibly Adzuna-truncated-to-500-chars) description at add-time,
-- to stay cheap for every passing job. app.salary_refresh periodically
-- re-attempts extraction using the full posting page for jobs still
-- missing a real salary -- this timestamp is what makes that safe to run
-- repeatedly without re-fetching the same job's page forever, the same
-- "check once" pattern as financial_data_last_checked/startuphub_last_checked
-- (migration 0004) and link_checked_at (migration 0005).
ALTER TABLE jobs ADD COLUMN salary_checked_at TIMESTAMP;
