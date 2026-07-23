import pytest

from app.resume import ResumeNotFoundError, get_base_resume_text


def test_raises_when_no_default_candidates_exist(tmp_path, monkeypatch):
    import app.resume as resume_module

    monkeypatch.setattr(
        resume_module,
        "DEFAULT_RESUME_CANDIDATES",
        [tmp_path / "base_resume.md", tmp_path / "base_resume.txt", tmp_path / "base_resume.docx"],
    )
    with pytest.raises(ResumeNotFoundError):
        get_base_resume_text()


def test_raises_for_explicit_missing_path(tmp_path):
    with pytest.raises(ResumeNotFoundError):
        get_base_resume_text(tmp_path / "does_not_exist.md")


def test_raises_for_empty_file(tmp_path):
    path = tmp_path / "empty.md"
    path.write_text("   \n  ", encoding="utf-8")
    with pytest.raises(ResumeNotFoundError):
        get_base_resume_text(path)


def test_reads_markdown_resume(tmp_path):
    path = tmp_path / "resume.md"
    path.write_text("# Jane Doe\n\nSolutions Architect with 10 years experience.", encoding="utf-8")
    text = get_base_resume_text(path)
    assert "Jane Doe" in text
    assert "Solutions Architect" in text
