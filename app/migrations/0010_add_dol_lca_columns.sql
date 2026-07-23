-- Real historical H-1B/H-1B1/E-3 sponsorship signal, from DOL/OFLC's public
-- LCA disclosure data -- distinct from visa_flag/visa_snippet (which only
-- ever read one specific posting's own text, never a company's real filing
-- history). See README.md's Roadmap and RUNBOOK.md for the full design
-- discussion.
--
-- dol_lca_employer_name: the exact employer name string matched in the DOL
-- LCA disclosure file, stored so a future refresh can try an exact match
-- against this cached name before falling back to fuzzy matching again, and
-- so a wrong match is visible/auditable rather than a silent black box.
--
-- last_lca_certified_date: the most recent LCA case decision date found for
-- this company with a "Certified" status -- i.e. "last sponsored in year X."
-- Deliberately a real date, not another boolean/count like the existing
-- h1b_sponsor_last_5yrs/h1b_petitions_last_5yrs columns (still unused,
-- reserved for this exact feature, left untouched here) -- a date lets any
-- future filter/display logic decide its own recency window instead of
-- baking in "last 5 years" at write time.
ALTER TABLE companies ADD COLUMN dol_lca_employer_name TEXT;
ALTER TABLE companies ADD COLUMN last_lca_certified_date TIMESTAMP;
