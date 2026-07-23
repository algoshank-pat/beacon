# Publishing to GitHub — Step-by-Step Guide

A complete checklist for pushing Beacon as a public portfolio piece without leaking any credentials or personal data.

---

## STEP 1 — Audit for Hardcoded Secrets

Before touching Git, scan the codebase for anything sensitive.

```powershell
# Search every Python/config file for secret-shaped assignments
Select-String -Path "app\*.py","app\**\*.py","*.toml","*.cfg","*.ini" `
  -Pattern "password|secret|api_key|apikey|token|credential" `
  -CaseSensitive:$false
```

**What you should find**: nothing real. Every credential in this codebase is loaded from `.env` (via `app/config.py`) or from the service-account JSON file path — never hardcoded in a `.py` file. If the search above turns up a real-looking value (not just a variable *name* like `api_key`), stop and fix it before continuing.

---

## STEP 2 — Verify `.gitignore` Is Complete

This repo's `.gitignore` already excludes:

```
.env                          # real API keys and Sheet IDs
service_account.json          # Google service-account private key
*.db / *.db-wal / *.db-shm    # the live database — real scraped jobs + your decisions
resumes/                       # your actual resume file
scheduler.log                  # operational log (not secret, just noisy/large)
scheduler.lock                 # runtime lock file
.venv/ __pycache__/ .pytest_cache/ .claude/
```

**Double-check nothing sensitive is staged:**
```powershell
git init          # if not already a repo
git add .
git status
# Review the full list carefully. If anything sensitive appears:
git restore --staged <filename>
# ...then add that filename/pattern to .gitignore before re-adding.
```

**Sanity check `.env.example` and `seed_companies.example.yaml`** — confirm they contain only placeholder text, never a real key or a real target company you don't want to broadcast:
```powershell
Get-Content .env.example
Get-Content seed_companies.example.yaml
```

---

## STEP 3 — If a Secret Was Accidentally Committed (History Scrub)

> ⚠️ Only do this if a real secret made it into Git history. Skip to Step 4 otherwise.

```powershell
# Install git-filter-repo
pip install git-filter-repo

# Remove a specific file from all history
git filter-repo --path service_account.json --invert-paths
git filter-repo --path .env --invert-paths

# Force-push all branches afterward
git push origin --force --all
```

**After scrubbing**: rotate every exposed credential immediately —
- Anthropic: revoke and reissue the key at console.anthropic.com
- Google service account: delete the exposed key and generate a new one in Cloud Console (IAM → Service Accounts → Keys)
- Adzuna/FMP/StartupHub: regenerate from each provider's dashboard

A history scrub only protects future clones — anyone who already has the old commit has the old secret. Rotating is the only real fix.

---

## STEP 4 — Create the GitHub Repository

1. Go to **https://github.com/new**
2. Fill in:
   - **Repository name**: `beacon` (or your choice)
   - **Description**: `A personal job-search pipeline that screens out visa-sponsorship dead ends automatically and surfaces matches in Google Sheets, for pennies.`
   - **Visibility**: Public
   - **Do NOT** initialize with a README/.gitignore/license — this repo already has all three
3. Click **Create repository** and copy the URL (e.g. `https://github.com/algoshank-pat/beacon.git`)

---

## STEP 5 — Initialize Git and Push

```powershell
cd "C:\AI\beacon"

git init
git branch -M main
git add .
git status
# Confirm the staged list looks right — no .env, no service_account.json, no *.db

git commit -m "Initial commit: Beacon v1.0

Personal job-search automation pipeline: multi-source ingestion
(Adzuna + Greenhouse/Lever/Ashby/SmartRecruiters), live-editable
filter criteria, three-tier visa-sponsorship classification
(regex -> free keyword check -> Claude Haiku for the ambiguous
remainder only), opt-in Claude Sonnet fit-scoring, free-only company
enrichment, and Google Sheets as the entire tracking/approval/
notification UI."

git remote add origin https://github.com/algoshank-pat/beacon.git
git push -u origin main
```

---

## STEP 6 — Add Screenshots

Fill in `docs/screenshots/` with real (redacted where noted) captures:

| Filename | What to capture |
|---|---|
| `docs/screenshots/beacon_sheet.png` | The Beacon sheet — company/title/visa flag/score columns visible, Decision/My Decision columns blurred or cropped out |
| `docs/screenshots/job_log_sheet.png` | The Job Log sheet with a few excluded jobs and their rejection reasons |
| `docs/screenshots/database_schema.png` / `database_data.png` | A DB-browser view of the SQLite schema and a sample query — both already added |
| `docs/screenshots/source_greenhouse.png` | A real public company's Greenhouse job board (no privacy concern — it's already public) |

```powershell
git add docs/screenshots/
git commit -m "Add README screenshots"
git push
```

---

## STEP 7 — Polish the GitHub Repository Page

1. **About** (gear icon, top-right of the repo page):
   - Description: `Job search automation for H-1B & visa holders — screens out sponsorship dead ends automatically, AI only where it's actually needed`
   - Topics: `h1b` `visa-sponsorship` `job-search` `international-students` `opt` `stem-opt` `immigration` `python` `automation` `google-sheets` `claude-api` `anthropic` `apscheduler` `sqlite`
2. **Pin the repository** on your GitHub profile
3. **Create a Release**:
   ```powershell
   git tag -a v1.0.0 -m "v1.0.0 — initial public release"
   git push origin v1.0.0
   ```

---

## Quick Checklist Before Pushing

```
[ ] No real API keys/passwords in any .py/.json/.toml/.yaml file
[ ] .gitignore covers .env, service_account.json, *.db*, resumes/, scheduler.log
[ ] .env.example and seed_companies.example.yaml have placeholder values only
[ ] `git status` after `git add .` shows nothing sensitive
[ ] README.md has real content, not template placeholders
[ ] docs/screenshots/ has real (redacted where needed) images
[ ] Repository name and description are clean and professional
```

---

## File Map: What Gets Committed vs. What Stays Local

| File | Committed | Reason |
|---|---|---|
| `app/` | ✅ | Core pipeline code |
| `tests/` | ✅ | Test suite — uses fakes, no live API calls |
| `scripts/` | ✅ | One-off maintenance scripts (no secrets embedded) |
| `README.md`, `docs/` | ✅ | Documentation |
| `RUNBOOK.md`, `job-search-app-prompt.md`, `job-search-app-technical-spec.md` | ✅ | Full build history and design rationale |
| `requirements.txt`, `pyproject.toml` | ✅ | Reproducible installs |
| `.env.example` | ✅ | Safe placeholder template |
| `seed_companies.example.yaml` | ✅ | Safe placeholder template |
| `seed_filters.yaml` | ✅ | Default filter criteria — no secrets |
| `.gitignore`, `LICENSE` | ✅ | |
| `.env` | ❌ | Real API keys and Sheet IDs |
| `service_account.json` | ❌ | Real Google service-account private key |
| `job_search.db` (+`-wal`/`-shm`) | ❌ | Real scraped data + your personal decisions, 400MB+ |
| `resumes/` | ❌ | Your actual resume |
| `scheduler.log` | ❌ | Large, no ongoing value to a reader |
| `seed_companies.yaml` | ❌ No | Your real target-company list — no secrets, but kept private by choice (gitignored); only `seed_companies.example.yaml` is published |
| `.venv/` | ❌ | Gigabytes of dependencies |
