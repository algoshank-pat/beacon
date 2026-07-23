-- Tracks which ingestion source produced this row ('greenhouse' | 'lever' | 'ashby' | 'adzuna').
-- Needed to correctly scope closed-job detection (diffing a board's current fetch against
-- previously-ingested rows for that same source) and cross-source fuzzy dedup, since a company
-- can have jobs land in the table from more than one source (e.g. Adzuna broad discovery AND a
-- targeted Ashby/Greenhouse board for the same company).
ALTER TABLE jobs ADD COLUMN source_type TEXT;

CREATE INDEX idx_jobs_company_source ON jobs(company_id, source_type);
