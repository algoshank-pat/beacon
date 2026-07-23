from app.sources.adzuna import fetch_adzuna_jobs_for_keyword
from app.sources.ashby import fetch_ashby_jobs
from app.sources.greenhouse import fetch_greenhouse_jobs
from app.sources.lever import fetch_lever_jobs
from app.sources.smartrecruiters import fetch_smartrecruiters_jobs
from tests.fakes import FakeResponse, FakeSession


def test_fetch_adzuna_jobs_normalizes_results():
    payload = {
        "results": [
            {
                "title": "Solutions Architect",
                "redirect_url": "https://adzuna.com/job/1",
                "description": "Great role",
                "location": {"display_name": "Remote"},
                "created": "2026-06-01T00:00:00Z",
                "company": {"display_name": "Acme Corp"},
                "salary_min": 150000,
                "salary_max": 190000,
            }
        ]
    }
    session = FakeSession([FakeResponse(200, payload)])
    jobs = fetch_adzuna_jobs_for_keyword("id", "key", "Solutions Architect", session=session)

    assert len(jobs) == 1
    job = jobs[0]
    assert job["title"] == "Solutions Architect"
    assert job["url"] == "https://adzuna.com/job/1"
    assert job["company_name"] == "Acme Corp"
    # salary_min/max must stay unset here -- reserved exclusively for a range
    # parsed from real JD text later (app.salary_extraction), never Adzuna's
    # own estimate. Real bug hit live: this field held Adzuna's estimate
    # (frequently wrong vs. what the posting actually stated) and never got
    # corrected for jobs whose real description text didn't restate a salary,
    # showing a fabricated number on Beacon as if it were the real range.
    assert job["salary_min"] is None
    assert job["salary_max"] is None
    assert job["salary_source"] is None
    assert job["adzuna_salary_min"] == 150000
    assert job["adzuna_salary_max"] == 190000
    assert job["source_type"] == "adzuna"

    method, url, kwargs = session.calls[0]
    assert kwargs["params"]["what_phrase"] == "Solutions Architect"


def test_fetch_adzuna_jobs_includes_where_when_location_set():
    session = FakeSession([FakeResponse(200, {"results": []})])
    fetch_adzuna_jobs_for_keyword("id", "key", "Sales Engineer", location="Austin, TX", session=session)
    _, _, kwargs = session.calls[0]
    assert kwargs["params"]["where"] == "Austin, TX"


def test_fetch_adzuna_jobs_omits_where_when_no_location():
    session = FakeSession([FakeResponse(200, {"results": []})])
    fetch_adzuna_jobs_for_keyword("id", "key", "Sales Engineer", session=session)
    _, _, kwargs = session.calls[0]
    assert "where" not in kwargs["params"]


def test_fetch_greenhouse_jobs_normalizes_results():
    payload = {
        "jobs": [
            {
                "title": "Presales Engineer",
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                "content": "<p>Join us</p>",
                "location": {"name": "New York, NY"},
                "first_published": "2026-05-01T00:00:00Z",
                "company_name": "Acme",
            }
        ]
    }
    session = FakeSession([FakeResponse(200, payload)])
    jobs = fetch_greenhouse_jobs("acme", session=session)

    assert len(jobs) == 1
    job = jobs[0]
    assert job["title"] == "Presales Engineer"
    assert job["location"] == "New York, NY"
    assert job["source_type"] == "greenhouse"
    assert job["description_html"] == "<p>Join us</p>"


def test_fetch_lever_jobs_normalizes_results():
    payload = [
        {
            "text": "Solutions Engineer",
            "hostedUrl": "https://jobs.lever.co/acme/1",
            "applyUrl": "https://jobs.lever.co/acme/1/apply",
            "description": "<p>Role details</p>",
            "categories": {"location": "Remote"},
            "createdAt": 1750000000000,
        }
    ]
    session = FakeSession([FakeResponse(200, payload)])
    jobs = fetch_lever_jobs("acme", session=session)

    assert len(jobs) == 1
    job = jobs[0]
    assert job["title"] == "Solutions Engineer"
    assert job["url"] == "https://jobs.lever.co/acme/1"
    assert job["apply_url"] == "https://jobs.lever.co/acme/1/apply"
    assert job["location"] == "Remote"
    assert job["posted_at"] is not None
    assert job["source_type"] == "lever"


def test_fetch_ashby_jobs_normalizes_results():
    payload = {
        "jobs": [
            {
                "title": "Sales Engineer, Enterprise",
                "jobUrl": "https://jobs.ashbyhq.com/acme/1",
                "applyUrl": "https://jobs.ashbyhq.com/acme/1/application",
                "descriptionHtml": "<p>Details</p>",
                "location": "NAMER",
                "publishedAt": "2026-04-22T15:41:06.460+00:00",
            }
        ],
        "apiVersion": "1",
    }
    session = FakeSession([FakeResponse(200, payload)])
    jobs = fetch_ashby_jobs("acme", session=session)

    assert len(jobs) == 1
    job = jobs[0]
    assert job["title"] == "Sales Engineer, Enterprise"
    assert job["url"] == "https://jobs.ashbyhq.com/acme/1"
    assert job["location"] == "NAMER"
    assert job["source_type"] == "ashby"


def test_fetch_ashby_jobs_handles_empty_board():
    session = FakeSession([FakeResponse(200, {"jobs": [], "apiVersion": "1"})])
    jobs = fetch_ashby_jobs("boomi", session=session)
    assert jobs == []


def _sr_stub(job_id, name="Solutions Architect", location="Austin, TX", company="Visa"):
    return {
        "id": job_id,
        "name": name,
        "location": {"fullLocation": location} if location else {},
        "releasedDate": "2026-05-01T00:00:00Z",
        "company": {"name": company},
    }


def test_fetch_smartrecruiters_jobs_fetches_description_for_new_posting():
    list_payload = {"content": [_sr_stub("123")], "totalFound": 1}
    detail_payload = {
        "jobAd": {
            "sections": {
                "jobDescription": {"text": "<p>Details</p>"},
                "qualifications": {"text": ""},
            }
        }
    }
    session = FakeSession([FakeResponse(200, list_payload), FakeResponse(200, detail_payload)])
    jobs = fetch_smartrecruiters_jobs("visa", session=session)

    assert len(jobs) == 1
    job = jobs[0]
    assert job["title"] == "Solutions Architect"
    assert job["url"] == "https://jobs.smartrecruiters.com/visa/123"
    assert job["apply_url"] == job["url"]
    assert job["location"] == "Austin, TX"
    assert job["source_type"] == "smartrecruiters"
    assert job["description_html"] == "<p>Details</p>"
    assert len(session.calls) == 2  # one list call, one detail call


def test_fetch_smartrecruiters_jobs_skips_detail_call_for_known_url():
    list_payload = {"content": [_sr_stub("123")], "totalFound": 1}
    session = FakeSession([FakeResponse(200, list_payload)])

    jobs = fetch_smartrecruiters_jobs(
        "visa", session=session, known_urls={"https://jobs.smartrecruiters.com/visa/123"}
    )

    assert jobs[0]["description_html"] == ""
    assert len(session.calls) == 1  # no detail call made


def test_fetch_smartrecruiters_jobs_paginates():
    page1 = {"content": [_sr_stub(str(i)) for i in range(100)], "totalFound": 150}
    page2 = {"content": [_sr_stub(str(i)) for i in range(100, 150)], "totalFound": 150}
    known_urls = {f"https://jobs.smartrecruiters.com/acme/{i}" for i in range(150)}
    session = FakeSession([FakeResponse(200, page1), FakeResponse(200, page2)])

    jobs = fetch_smartrecruiters_jobs("acme", session=session, known_urls=known_urls)

    assert len(jobs) == 150
    assert session.calls[0][2]["params"] == {"limit": 100, "offset": 0}
    assert session.calls[1][2]["params"] == {"limit": 100, "offset": 100}


def test_fetch_smartrecruiters_jobs_handles_empty_board():
    session = FakeSession([FakeResponse(200, {"content": [], "totalFound": 0})])
    jobs = fetch_smartrecruiters_jobs("acme", session=session)
    assert jobs == []
