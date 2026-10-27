# Simple PR Analyzer

A small FastAPI server: GitHub PR URL → GitHub features → Gemini → validated JSON.
No ORM, frontend framework, background workers, or separate database server.

## Run

Python 3.10 or newer:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
# Only if .env does not exist yet:
Copy-Item .env.example .env
.venv\Scripts\python run.py
```

For later starts, just run `.venv\Scripts\python run.py`, or `python run.py` with
the virtual environment activated. The server runs on port 8000 with auto-reload.

Set `GITHUB_TOKEN` and `GEMINI_TOKEN` in `.env`. The original spelling `Gemini_TOKEN`
is also accepted. `GEMINI_MODEL` can be changed to a model available to your key.
The supplied credentials are already saved in the local `.env`, which Git ignores.

Open **http://127.0.0.1:8000/admin** and sign in with **admin@12354**.
Create a named API token and copy it; the full token is displayed only once.
Suspend, reactivate, or delete tokens from this page. Tokens persist in `tokens.db`
across restarts; only their hashes are stored. `ADMIN_PASSWORD` overrides the default.

Interactive API docs: **http://127.0.0.1:8000/docs**. Click **Authorize** and paste
your API token (without the `Bearer` prefix) to try `/analyze`.

## Frontend request

```javascript
const response = await fetch('http://127.0.0.1:8000/analyze', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${apiToken}`
  },
  body: JSON.stringify({pr_url: 'https://github.com/owner/repo/pull/123'})
});
const data = await response.json();
if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Invalid request');
console.log(data.analysis);
```

`CORS_ORIGINS` is a comma-separated list of frontend origins; localhost ports 3000
and 5173 are allowed by default. Add your frontend's exact origin if different.

The response contains `pr_url`, `features`, `analysis`, and `probability_note`.
Example `analysis` (illustrative, not an actual PR evaluation):

```json
{
  "merge_probability": 78,
  "quality_score": 80,
  "quality": "good",
  "confidence": "medium",
  "summary": "Focused change with a clear description and one missing edge-case test.",
  "merge_reasoning": "One approval and passing CI support merging; an edge case remains untested.",
  "recommendations": {
    "excellent": [],
    "good": ["The change has a small, clearly explained scope."],
    "bad": ["The empty-input branch has no visible test."],
    "worst": []
  },
  "potential_improvements": ["Add an empty-input test for the changed parser."],
  "limitations": ["Only the supplied diff was inspected; tests were not run."]
}
```

Scores are 0–100. Quality bands: excellent ≥85, good ≥65, bad ≥40, worst <40.
Merge probability is an AI estimate, not a calibrated prediction. Already merged
PRs return 100; currently closed, unmerged PRs return 0. Quality is still assessed.

## What is collected

PR title, description, author, state, draft flag, mergeability, timestamps, labels,
head SHA, base branch, additions, deletions, file count, commit count, comment
counts, file patches, discussion comments, inline review comments, reviews,
review decisions, commit messages, checks, and commit statuses.

Lists are paginated, capped at 3,000 items each. GitHub also imposes its own limits
(including at most 3,000 files and 250 PR commits). Text sent to Gemini is capped
at 180,000 characters, with smaller per-field limits. Missing patches, unavailable
checks, truncation, and unknown mergeability are reported in `features.warnings`.
Unavailable checks/statuses are `null`; an empty array means none were found.
This does not fetch entire source files, branch protection rules, or run tests.

For private repositories, the GitHub token needs repository access and read
permissions for pull requests, issues, contents, checks, and commit statuses.
The collected PR data is sent to Gemini for analysis.

Gemini receives a custom system prompt and JSON schema. Pydantic rejects missing
fields, extra fields, wrong types, invalid scores/categories, and inconsistent
quality bands. Invalid, blocked, or incomplete output is retried once. A second
failure returns HTTP 502, never a fabricated analysis.

Errors use `{"detail": "message"}` (input validation errors may contain a list):

- 401: missing, invalid, or suspended API token; wrong admin password.
- 404: inaccessible/missing PR or missing managed token.
- 422: invalid request or PR URL.
- 502: upstream error or invalid Gemini response after retry.
- 503: missing Gemini configuration, upstream quota/rate limit, or Gemini temporarily unavailable.
- 504: GitHub or Gemini timeout.

Admin endpoints use `X-Admin-Password`: `GET/POST /admin/tokens`,
`PATCH /admin/tokens/{id}` with `{"active": false}` (or `true`), and
`DELETE /admin/tokens/{id}`. Token creation accepts `{"name": "My frontend"}`.

## Files and tests

- `run.py`: start the development server.
- `main.py`: routes, authentication, SQLite token storage.
- `github_service.py`: GitHub collection and named features.
- `gemini_service.py`: editable prompt, schema, validation, and retry.
- `admin.html`: plain HTML/JavaScript token manager.
- `test_app.py`: offline integration tests using mocked upstream APIs.

```powershell
.venv\Scripts\python -m pip install pytest
.venv\Scripts\python -m pytest -q
```

API references: [GitHub pull requests](https://docs.github.com/en/rest/pulls/pulls),
[GitHub statuses](https://docs.github.com/en/rest/commits/statuses), and
[Gemini structured output](https://ai.google.dev/gemini-api/docs/generate-content/structured-output).
