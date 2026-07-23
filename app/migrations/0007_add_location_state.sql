-- Computed once at ingestion time by app.location_state.resolve_location_state
-- from the job's free-text `location` string. A 2-letter US state
-- abbreviation, "Remote-USA" for remote/nationwide/multi-state postings, or
-- NULL when the location couldn't be confidently resolved (left blank
-- rather than guessed).
ALTER TABLE jobs ADD COLUMN location_state TEXT;
