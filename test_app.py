"""Offline tests: no real credentials, network requests, or paid Gemini calls."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import github_service
import main

URL = "https://github.com/demo/project/pull/12"
ADMIN = {"X-Admin-Password": "test-password"}
VALID = {
    "merge_probability": 75, "quality_score": 80, "quality": "good", "confidence": "medium",
    "summary": "Focused change.", "merge_reasoning": "Approved with passing CI.",
    "recommendations": {"excellent": [], "good": ["Small diff."], "bad": [], "worst": []},
    "potential_improvements": ["Add an edge-case test."], "limitations": ["Tests were not run."],
}


def gemini_reply(value=VALID):
    return {"candidates": [{"finishReason": "STOP", "content": {"parts": [
        {"text": json.dumps(value) if not isinstance(value, str) else value}
    ]}}]}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "tokens.db")
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("GITHUB_TOKEN", "test-github")
    monkeypatch.setenv("GEMINI_TOKEN", "test-gemini")
    with TestClient(main.app) as client:
        yield client


@pytest.fixture
def auth(client):
    response = client.post("/admin/tokens", headers=ADMIN, json={"name": "Test"})
    assert response.status_code == 201
    return {"Authorization": "Bearer " + response.json()["token"]}


@pytest.fixture
def upstream(monkeypatch):
    state = {"calls": [], "gemini": [gemini_reply()], "errors": {}, "merged": False, "state": "open"}

    def handler(request):
        state["calls"].append(request)
        if request.url.host == "generativelanguage.googleapis.com":
            assert "authorization" not in request.headers  # GitHub token never goes to Gemini.
            assert request.headers["x-goog-api-key"] == "test-gemini"
            value = state["gemini"].pop(0) if len(state["gemini"]) > 1 else state["gemini"][0]
            return value if isinstance(value, httpx.Response) else httpx.Response(200, json=value)
        assert request.url.host == "api.github.com"
        assert request.headers["Authorization"] == "Bearer test-github"
        path = request.url.path.removeprefix("/repos/demo/project")
        if path in state["errors"]:
            error = state["errors"][path]
            if isinstance(error, Exception):
                raise error
            return error
        page = request.url.params.get("page", "1")
        headers = {}
        if path == "/pulls/12":
            data = {"title": "Fix parser", "body": "Handle input", "head": {"sha": "abc"},
                    "base": {"ref": "main"}, "user": {"login": "author"},
                    "state": state["state"], "merged": state["merged"], "draft": False,
                    "mergeable": True, "mergeable_state": "clean", "created_at": "2026-10-01T00:00:00Z",
                    "updated_at": "2026-10-02T00:00:00Z", "additions": 5, "deletions": 2,
                    "changed_files": 2, "commits": 1, "comments": 1, "review_comments": 1}
        elif path == "/pulls/12/files":
            data = [{"filename": f"file{page}.py", "status": "modified", "additions": 3,
                     "deletions": 1, "changes": 4, "patch": "@@\n-old\n+new"}]
            if page == "1":
                headers["Link"] = '<https://api.github.com/repos/demo/project/pulls/12/files?page=2>; rel="next"'
        elif path == "/pulls/12/reviews":
            data = [{"user": {"login": "reviewer"}, "state": s, "body": "Review", "commit_id": "abc"}
                    for s in ["CHANGES_REQUESTED", "APPROVED", "COMMENTED"]]
        elif path in {"/issues/12/comments", "/pulls/12/comments"}:
            data = [{"user": {"login": "reviewer"}, "body": "Looks good", "path": "file1.py", "line": 3}]
        elif path == "/pulls/12/commits":
            data = [{"sha": "abc", "commit": {"message": "Fix parser"}}]
        elif path == "/commits/abc/check-runs":
            data = {"check_runs": [{"name": "tests", "status": "completed", "conclusion": "success"}]}
        elif path == "/commits/abc/status":
            data = {"statuses": [{"context": "lint", "state": "success", "description": "Pass"}]}
        else:
            pytest.fail(f"Unexpected GitHub endpoint: {path}")
        return httpx.Response(200, json=data, headers=headers)

    original = httpx.AsyncClient
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    return state


def test_token_lifecycle(client, upstream):
    for method, path, body in [("GET", "/admin/tokens", None), ("POST", "/admin/tokens", {"name": "x"}),
                               ("PATCH", "/admin/tokens/1", {"active": False}), ("DELETE", "/admin/tokens/1", None)]:
        assert client.request(method, path, json=body).status_code == 401
    assert client.get("/admin/tokens", headers={"X-Admin-Password": "wrong"}).status_code == 401
    created = client.post("/admin/tokens", headers=ADMIN, json={"name": "Browser"}).json()
    auth = {"Authorization": "Bearer " + created["token"]}
    path = f"/admin/tokens/{created['id']}"
    assert created["token"] not in main.DB_PATH.read_bytes().decode(errors="ignore")
    assert "token_hash" not in client.get("/admin/tokens", headers=ADMIN).text
    with TestClient(main.app) as restarted:
        assert len(restarted.get("/admin/tokens", headers=ADMIN).json()) == 1
    assert client.post("/analyze", headers=auth, json={"pr_url": URL}).status_code == 200
    assert client.patch(path, headers=ADMIN, json={"active": False}).status_code == 200
    upstream["calls"].clear()
    assert client.post("/analyze", headers=auth, json={"pr_url": URL}).status_code == 401
    assert not upstream["calls"]
    assert client.patch(path, headers=ADMIN, json={"active": True}).status_code == 200
    assert client.post("/analyze", headers=auth, json={"pr_url": URL}).status_code == 200
    assert client.delete(path, headers=ADMIN).status_code == 204
    assert client.post("/analyze", headers=auth, json={"pr_url": URL}).status_code == 401
    assert client.delete(path, headers=ADMIN).status_code == 404
    assert client.patch(path, headers=ADMIN, json={"active": True}).status_code == 404


def test_validation_and_public_pages(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/admin").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.post("/analyze", json={"pr_url": URL}).status_code == 401
    assert client.post("/admin/tokens", headers=ADMIN, json={"name": "  "}).status_code == 422
    assert client.patch("/admin/tokens/1", headers=ADMIN, json={"active": "false"}).status_code == 422


@pytest.mark.parametrize("url", ["http://github.com/a/b/pull/1", "https://evil.com/a/b/pull/1",
    "https://github.com@evil.com/a/b/pull/1", "https://github.com/a/b/issues/1",
    "https://github.com/a/../pull/1", "https://github.com/a/b/pull/0", "https://[invalid"])
def test_bad_urls(client, auth, upstream, url):
    assert client.post("/analyze", headers=auth, json={"pr_url": url}).status_code == 422
    assert not upstream["calls"]


def test_analysis_pipeline(client, auth, upstream):
    response = client.post("/analyze", headers=auth, json={"pr_url": URL + "?diff=split#discussion"})
    assert response.status_code == 200, response.text
    data = response.json()
    features = data["features"]
    assert data["pr_url"] == URL
    assert data["analysis"] == VALID
    assert len(features["files"]) == 2
    assert features["additions"] == 5 and features["deletions"] == 2
    assert features["approvals"] == 1 and features["changes_requested"] == 0
    assert features["comments"][0]["body"] == "Looks good"
    assert features["review_comments"][0]["line"] == 3
    assert features["checks"][0]["conclusion"] == "success"
    sent = json.loads(upstream["calls"][-1].content)
    assert json.loads(sent["contents"][0]["parts"][0]["text"]) == features
    assert sent["generationConfig"]["responseFormat"]["text"]["schema"]["required"]


@pytest.mark.parametrize("merged,state,probability", [(True, "closed", 100), (False, "closed", 0)])
def test_known_outcomes(client, auth, upstream, merged, state, probability):
    upstream.update(merged=merged, state=state)
    result = client.post("/analyze", headers=auth, json={"pr_url": URL}).json()
    assert result["analysis"]["merge_probability"] == probability


@pytest.mark.parametrize("invalid", ["not JSON", {}, {**VALID, "merge_probability": 120},
    {**VALID, "quality_score": "80"}, {**VALID, "quality": "excellent"}, {**VALID, "confidence": "certain"},
    {**VALID, "summary": "  "}, {**VALID, "unexpected": True}, {**VALID, "potential_improvements": "fix it"}])
def test_invalid_gemini_retries(client, auth, upstream, invalid):
    upstream["gemini"] = [gemini_reply(invalid), gemini_reply()]
    assert client.post("/analyze", headers=auth, json={"pr_url": URL}).status_code == 200
    assert sum(r.url.host == "generativelanguage.googleapis.com" for r in upstream["calls"]) == 2


@pytest.mark.parametrize("reply", [{}, {"candidates": []}, {"candidates": [{"finishReason": "MAX_TOKENS"}]},
    gemini_reply("```json\n{}\n```"), httpx.Response(200, text="bad JSON")])
def test_repeated_invalid_gemini_fails_cleanly(client, auth, upstream, reply):
    upstream["gemini"] = [reply]
    response = client.post("/analyze", headers=auth, json={"pr_url": URL})
    assert response.status_code == 502
    assert "invalid response twice" in response.json()["detail"]


@pytest.mark.parametrize("response,expected", [(httpx.Response(404), 404), (httpx.Response(401), 502),
    (httpx.Response(403, headers={"x-ratelimit-remaining": "0"}), 503), (httpx.Response(500), 502),
    (httpx.ReadTimeout("timeout"), 504), (httpx.ConnectError("offline"), 502)])
def test_github_errors(client, auth, upstream, response, expected):
    upstream["errors"]["/pulls/12"] = response
    assert client.post("/analyze", headers=auth, json={"pr_url": URL}).status_code == expected
    assert len(upstream["calls"]) == 1


def test_missing_checks_and_collection_limits(client, auth, upstream, monkeypatch):
    upstream["errors"]["/commits/abc/check-runs"] = httpx.Response(403)
    monkeypatch.setattr(github_service, "MAX_PAGES", 1)
    response = client.post("/analyze", headers=auth, json={"pr_url": URL})
    assert response.status_code == 200
    features = response.json()["features"]
    assert features["checks"] is None
    assert any("capped" in warning for warning in features["warnings"])
    assert any("unavailable" in warning for warning in features["warnings"])


@pytest.mark.parametrize("code,expected", [(400, 502), (403, 502), (429, 503), (500, 503), (503, 503)])
def test_gemini_http_errors(client, auth, upstream, code, expected):
    upstream["gemini"] = [httpx.Response(code)]
    response = client.post("/analyze", headers=auth, json={"pr_url": URL})
    assert response.status_code == expected
    assert "test-gemini" not in response.text


def test_cors(client):
    response = client.options("/analyze", headers={"Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
