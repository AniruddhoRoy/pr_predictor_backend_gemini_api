"""Collect named GitHub features, following pagination for every list."""

import os
import re
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

MAX_PAGES = 30


def parse_pr_url(url):
    try:
        parsed = urlsplit(url.strip())
        match = re.fullmatch(r"/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)/?", parsed.path)
        if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or not match:
            raise ValueError
        owner, repo, number = match.groups()
        if repo in {".", ".."}:
            raise ValueError
        return owner, repo, int(number)
    except ValueError:
        raise HTTPException(422, "Use a URL like https://github.com/owner/repo/pull/123.") from None


async def collect_features(client, pr_url):
    owner, repo, number = parse_pr_url(pr_url)
    base = f"https://api.github.com/repos/{owner}/{repo}"
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"}
    if os.getenv("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    warnings = []
    text_budget = 180_000

    def clip(value, limit=4000):
        nonlocal text_budget
        text = value or ""
        kept = text[:min(limit, text_budget)]
        text_budget -= len(kept)
        if len(kept) < len(text):
            warnings.append("Some text or patches were shortened to fit the analysis budget.")
        return kept

    async def get(path, params=None, optional=False):
        try:
            response = await client.get(base + path, headers=headers, params=params)
        except httpx.TimeoutException:
            raise HTTPException(504, "GitHub request timed out.") from None
        except httpx.RequestError:
            raise HTTPException(502, "Could not connect to GitHub.") from None
        if response.status_code == 429 or (
            response.status_code == 403 and (response.headers.get("x-ratelimit-remaining") == "0"
                                            or response.headers.get("retry-after"))
        ):
            raise HTTPException(503, "GitHub rate limit reached. Try again later.")
        if optional and response.status_code in {403, 404}:
            warnings.append(f"GitHub data unavailable: {path}. Check token permissions.")
            return None
        if response.status_code == 404:
            raise HTTPException(404, "Pull request not found or GITHUB_TOKEN cannot access it.")
        if response.status_code in {401, 403}:
            raise HTTPException(502, "GitHub rejected the server's GITHUB_TOKEN or its permissions.")
        if not response.is_success:
            raise HTTPException(502, f"GitHub request failed (HTTP {response.status_code}).")
        try:
            return response.json(), "next" in response.links
        except ValueError:
            raise HTTPException(502, "GitHub returned invalid JSON.") from None

    async def listing(path, key=None, optional=False):
        items = []
        for page in range(1, MAX_PAGES + 1):
            result = await get(path, {"per_page": 100, "page": page}, optional)
            if result is None:
                return None
            data, has_next = result
            items.extend(data[key] if key else data)
            if not has_next:
                return items
        warnings.append(f"GitHub list capped at {MAX_PAGES * 100} items: {path}.")
        return items

    pr, _ = await get(f"/pulls/{number}")
    files = await listing(f"/pulls/{number}/files")
    comments = await listing(f"/issues/{number}/comments")
    reviews = await listing(f"/pulls/{number}/reviews")
    review_comments = await listing(f"/pulls/{number}/comments")
    commits = await listing(f"/pulls/{number}/commits")
    sha = pr["head"]["sha"]
    checks = await listing(f"/commits/{sha}/check-runs", "check_runs", optional=True)
    statuses = await listing(f"/commits/{sha}/status", "statuses", optional=True)

    # A later COMMENTED review does not cancel an earlier approval/change request.
    latest_reviews = {}
    for review in reviews:
        if review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest_reviews[(review.get("user") or {}).get("login", "deleted-user")] = review["state"]
    if len(files) < pr["changed_files"] or len(commits) < pr["commits"]:
        warnings.append("GitHub did not return all files or commits (API limits may apply).")
    if pr.get("mergeable") is None:
        warnings.append("GitHub has not determined mergeability yet.")
    if any(not file.get("patch") for file in files):
        warnings.append("Some files have no patch (binary files or GitHub diff limits).")

    def comment_data(comment):
        return {
            "author": (comment.get("user") or {}).get("login"),
            "body": clip(comment.get("body")),
            "created_at": comment.get("created_at"),
            "path": comment.get("path"), "line": comment.get("line"),
        }

    # Explicit feature variables sent to Gemini and returned to the frontend.
    features = {
        "pr_url": f"https://github.com/{owner}/{repo}/pull/{number}",
        "repository": f"{owner}/{repo}", "number": number,
        "title": clip(pr["title"]), "description": clip(pr.get("body"), 10000),
        "author": (pr.get("user") or {}).get("login"),
        "author_association": pr.get("author_association"),
        "state": pr["state"], "merged": pr["merged"], "draft": pr["draft"],
        "mergeable": pr.get("mergeable"), "mergeable_state": pr.get("mergeable_state"),
        "created_at": pr["created_at"], "updated_at": pr["updated_at"],
        "head_sha": sha, "base_branch": pr["base"]["ref"],
        "additions": pr["additions"], "deletions": pr["deletions"],
        "changed_files": pr["changed_files"], "commit_count": pr["commits"],
        "comment_count": pr["comments"], "review_comment_count": pr["review_comments"],
        "review_count": len(reviews),
        "approvals": list(latest_reviews.values()).count("APPROVED"),
        "changes_requested": list(latest_reviews.values()).count("CHANGES_REQUESTED"),
        "labels": [label["name"] for label in pr.get("labels", [])],
        "files": [{"filename": f["filename"], "status": f["status"],
                   "additions": f["additions"], "deletions": f["deletions"],
                   "changes": f["changes"], "patch": clip(f.get("patch"), 12000)} for f in files],
        "comments": [comment_data(c) for c in comments],
        "reviews": [{"author": (r.get("user") or {}).get("login"), "state": r["state"],
                     "body": clip(r.get("body")), "commit_id": r.get("commit_id"),
                     "submitted_at": r.get("submitted_at")} for r in reviews],
        "review_comments": [comment_data(c) for c in review_comments],
        "commits": [{"sha": c["sha"], "message": clip(c["commit"]["message"], 1000)} for c in commits],
        "checks": None if checks is None else [
            {"name": c["name"], "status": c["status"], "conclusion": c.get("conclusion")} for c in checks],
        "statuses": None if statuses is None else [
            {"context": s["context"], "state": s["state"], "description": clip(s.get("description"), 500)}
            for s in statuses],
    }
    features["warnings"] = list(dict.fromkeys(warnings))
    return features

