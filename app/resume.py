"""Base resume loading for fit scoring.

Tailored/registered resumes (from the Claude Desktop handoff, M7) are tracked
via fit_scores.resume_file_path — this module only handles the one base resume
used for initial scoring before any resume has been generated for a job.
"""
from __future__ import annotations

from pathlib import Path

from app.config import PROJECT_ROOT
from app.docx_text import extract_docx_text

RESUME_DIR = PROJECT_ROOT / "resumes"
DEFAULT_RESUME_CANDIDATES = [
    RESUME_DIR / "base_resume.md",
    RESUME_DIR / "base_resume.txt",
    RESUME_DIR / "base_resume.docx",
]


class ResumeNotFoundError(Exception):
    pass


def _read_resume_file(path: Path) -> str:
    if path.suffix.lower() == ".docx":
        return extract_docx_text(path)
    return path.read_text(encoding="utf-8")


def get_base_resume_text(path: str | Path | None = None) -> str:
    if path is not None:
        resume_path = Path(path)
        if not resume_path.exists():
            raise ResumeNotFoundError(f"Base resume not found at {resume_path}.")
    else:
        resume_path = next((p for p in DEFAULT_RESUME_CANDIDATES if p.exists()), None)
        if resume_path is None:
            candidates = ", ".join(str(p) for p in DEFAULT_RESUME_CANDIDATES)
            raise ResumeNotFoundError(f"No base resume found. Save one at one of: {candidates}")

    text = _read_resume_file(resume_path).strip()
    if not text:
        raise ResumeNotFoundError(f"Base resume at {resume_path} is empty.")
    return text
