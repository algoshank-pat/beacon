-- Companies I'm tracking (seed list I maintain + auto-added from postings)
CREATE TABLE companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    board_url TEXT,
    source_type TEXT,              -- 'greenhouse' | 'lever' | 'ashby' | 'adzuna' | 'manual'
    industry TEXT,
    company_size TEXT,
    priority_tier TEXT,            -- 'S' | 'A' | 'B' | 'C'
    is_favorite BOOLEAN DEFAULT 0,
    employee_count INTEGER,
    employee_count_range TEXT,
    founded_year INTEGER,
    hq_location TEXT,
    linkedin_company_url TEXT,
    famous_product TEXT,
    visa_sponsorship_history TEXT, -- 'known_sponsor' | 'known_non_sponsor' | 'unknown' (posting-language signal)
    h1b_sponsor_last_5yrs BOOLEAN,
    h1b_petitions_last_5yrs INTEGER,
    h1b_data_last_checked TIMESTAMP,
    company_type TEXT,             -- 'public' | 'private'
    funding_stage TEXT,            -- 'bootstrapped' | 'seed' | 'series_a' | 'series_b' | 'series_c' | 'series_d_plus' | 'ipo_public' | 'unknown'
    revenue_or_valuation TEXT,     -- free text: "$50M ARR (est.)" or "$12.4B market cap"
    revenue_valuation_source TEXT,
    financial_data_last_checked TIMESTAMP,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Every job posting ingested
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    company_id INTEGER REFERENCES companies(id),
    title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    apply_url TEXT,
    description TEXT,
    location TEXT,
    remote_type TEXT,
    seniority TEXT,
    job_function TEXT,
    salary_min INTEGER,
    salary_max INTEGER,
    salary_source TEXT,
    visa_flag TEXT,
    visa_snippet TEXT,
    duplicate_of_job_id INTEGER REFERENCES jobs(id),
    posted_at TIMESTAMP,
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notified_at TIMESTAMP,          -- set when Sheet row is first appended; drives stalled-decision reminders
    decision_processed_at TIMESTAMP,-- idempotency guard: once set, a decision has been acted on and won't be reprocessed
    status TEXT DEFAULT 'new',      -- 'new' | 'filtered_out' | 'scored' | 'notified' | 'approved' | 'rejected' | 'closed' | 'duplicate'
    rejection_reason TEXT,
    sheet_row_number INTEGER
);

-- One row per fit-scoring event (initial score + post-resume re-score)
CREATE TABLE fit_scores (
    id INTEGER PRIMARY KEY,
    job_id INTEGER REFERENCES jobs(id),
    resume_file_path TEXT,
    score INTEGER,
    gap_analysis TEXT,
    scored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- My notes on generated resumes — included in future Claude Desktop handoff prompts
CREATE TABLE resume_feedback (
    id INTEGER PRIMARY KEY,
    job_id INTEGER REFERENCES jobs(id),
    feedback TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Live-editable filter criteria (replaces static config.yaml at runtime)
CREATE TABLE filter_settings (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    value TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE filter_keywords (
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,
    keyword TEXT NOT NULL,
    is_active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(category, keyword)
);

-- Observability: every run of the polling/filter/score/notify/decision pipeline
CREATE TABLE workflow_runs (
    id INTEGER PRIMARY KEY,
    run_type TEXT NOT NULL DEFAULT 'main_pipeline', -- 'main_pipeline' | 'approval_poll'
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    status TEXT,
    jobs_ingested INTEGER DEFAULT 0,
    jobs_filtered_out INTEGER DEFAULT 0,
    jobs_scored INTEGER DEFAULT 0,
    jobs_notified INTEGER DEFAULT 0,
    decisions_processed INTEGER DEFAULT 0,
    tokens_used_input INTEGER DEFAULT 0,
    tokens_used_output INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0,
    error_summary TEXT
);

CREATE TABLE step_logs (
    id INTEGER PRIMARY KEY,
    workflow_run_id INTEGER REFERENCES workflow_runs(id),
    job_id INTEGER REFERENCES jobs(id),
    step_name TEXT,                 -- 'ingest' | 'filter' | 'visa_scan' | 'fit_score' | 'notify' | 'sheet_sync' | 'decision_poll' | 'reminder'
    step_status TEXT,
    detail TEXT,
    tokens_input INTEGER DEFAULT 0,
    tokens_output INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_jobs_company_id ON jobs(company_id);
CREATE INDEX idx_jobs_decision_processed_at ON jobs(decision_processed_at);
CREATE INDEX idx_fit_scores_job_id ON fit_scores(job_id);
CREATE INDEX idx_filter_keywords_category ON filter_keywords(category);
CREATE INDEX idx_step_logs_workflow_run_id ON step_logs(workflow_run_id);
CREATE INDEX idx_step_logs_job_id ON step_logs(job_id);
