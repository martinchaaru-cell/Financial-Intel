"""
Not exhaustive route coverage — a starting point that catches the
"app doesn't even boot" / "route 500s on an empty database" class of
regression, which is the most common thing CI is meant to catch before
a deploy. Extend per-route as each area gets real test coverage.
"""


def test_index_page_loads(client):
    resp = client.get("/")
    assert resp.status_code == 200


def test_system_status_reports_engine(client):
    resp = client.get("/api/system/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["database_engine"] == "sqlite"
    assert body["counts"]["companies"] == 0


def test_overview_on_empty_database_does_not_500(client):
    resp = client.get("/api/overview")
    assert resp.status_code == 200


def test_survey_overview_reports_no_data_on_empty_database(client):
    resp = client.get("/api/survey/overview")
    assert resp.status_code == 200
    assert resp.get_json().get("has_data") is False


def test_guest_cannot_create_company(client):
    """require_role() should reject an unauthenticated request server-side
    — the whole point of that decorator per its own docstring."""
    resp = client.post("/api/companies", json={"name": "Should Not Save"})
    assert resp.status_code in (401, 403)