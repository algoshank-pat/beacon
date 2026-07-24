"""One-shot bootstrap for a fresh Beacon checkout.

Creates the venv, installs dependencies into it, copies the example config
files (never overwriting anything that already exists), and runs the two
setup steps that need no credentials at all (`migrate`, `seed-filters`).

Deliberately does NOT run `seed-companies` or `pipeline` -- the former
would seed the example file's placeholder companies (Anthropic, OpenAI,
Google, Microsoft, Amazon) before you've had a chance to replace them with
your own targets, and the latter needs real API keys in `.env` that this
script can't create for you. Those, along with getting your own API keys,
sharing a Sheet with your service account, and placing your resume, are
the genuinely manual, one-time steps -- see README.md's Setup section.

Run with your system Python, before any venv exists:
    python bootstrap.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"


def _venv_python() -> Path:
    if sys.platform == "win32":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _step(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def create_venv() -> None:
    if VENV_DIR.exists():
        _step(f"Reusing existing virtual environment at {VENV_DIR}")
        return
    _step(f"Creating virtual environment at {VENV_DIR}")
    venv.create(VENV_DIR, with_pip=True)


def install_dependencies() -> None:
    _step("Installing dependencies (pip install -r requirements.txt)")
    subprocess.run(
        [str(_venv_python()), "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")],
        check=True,
    )


def copy_if_missing(template: str, target: str) -> None:
    template_path, target_path = ROOT / template, ROOT / target
    if target_path.exists():
        _step(f"{target} already exists, leaving it alone")
        return
    _step(f"Creating {target} from {template}")
    shutil.copy(template_path, target_path)


def run_credential_free_setup() -> None:
    _step("Creating the database schema")
    subprocess.run([str(_venv_python()), "-m", "app.cli", "migrate"], check=True, cwd=ROOT)
    _step("Loading default filter criteria from seed_filters.yaml")
    subprocess.run(
        [str(_venv_python()), "-m", "app.cli", "seed-filters", "--file", "seed_filters.yaml"],
        check=True, cwd=ROOT,
    )


def main() -> None:
    create_venv()
    install_dependencies()
    copy_if_missing(".env.example", ".env")
    copy_if_missing("seed_companies.example.yaml", "seed_companies.yaml")
    run_credential_free_setup()

    activate = r".venv\Scripts\activate" if sys.platform == "win32" else "source .venv/bin/activate"
    print(
        "\nDone. What's left needs your own accounts, so it can't be scripted:\n"
        f"  1. Activate the venv: {activate}\n"
        "  2. Fill in your API keys and Sheet IDs in .env (see README.md's Appendix for how to get each one)\n"
        "  3. Edit seed_companies.yaml -- replace the example companies with your own targets, or leave it empty\n"
        "  4. Place your resume at resumes/base_resume.docx (or .md / .txt)\n"
        "  5. Run: python -m app.cli seed-companies --file seed_companies.yaml\n"
        "  6. Run: python -m app.cli pipeline\n"
    )


if __name__ == "__main__":
    main()
