"""Run with: python run.py"""

import hashlib
import os
import secrets
import sqlite3
from contextlib import closing, asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, StringConstraints

from gemini_service import Analysis, analyze_with_gemini
from github_service import collect_features

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
DB_PATH = ROOT / "tokens.db"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin@12354")
bearer = HTTPBearer(auto_error=False)
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


def query(sql, values=()):
    # One small database, no ORM or separate database server.
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.row_factory = sqlite3.Row
        with db:
            cursor = db.execute(sql, values)
            return [dict(row) for row in cursor.fetchall()]


@asynccontextmanager
async def lifespan(app):
    query("""CREATE TABLE IF NOT EXISTS tokens (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
        prefix TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    yield


app = FastAPI(title="PR Analyzer", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[s.strip() for s in os.getenv(
        "CORS_ORIGINS", "http://localhost:3000,http://localhost:5173"
    ).split(",") if s.strip()],
    allow_methods=["POST"],
    allow_headers=["Authorization", "Content-Type"],
)


def require_admin(x_admin_password: str = Header(default="")):
    if not secrets.compare_digest(x_admin_password.encode(), ADMIN_PASSWORD.encode()):
        raise HTTPException(401, "Incorrect admin password.")


def require_token(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    digest = hashlib.sha256(credentials.credentials.encode()).hexdigest() if credentials else ""
    if not query("SELECT id FROM tokens WHERE token_hash = ? AND active = 1", (digest,)):
        raise HTTPException(401, "Invalid or suspended API token.", headers={"WWW-Authenticate": "Bearer"})


class TokenCreate(BaseModel):
    name: Name


class TokenUpdate(BaseModel):
    active: bool = Field(strict=True)


class AnalyzeRequest(BaseModel):
    pr_url: str = Field(min_length=1, max_length=2048)


class AnalyzeResponse(BaseModel):
    pr_url: str
    features: dict
    analysis: Analysis
    probability_note: str = "AI estimate, not a calibrated probability or a guarantee."


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(ROOT / "admin.html", headers={"Cache-Control": "no-store"})


@app.get("/admin/tokens", dependencies=[Depends(require_admin)])
def list_tokens():
    return query("SELECT id, name, prefix, active, created_at FROM tokens ORDER BY id DESC")


@app.post("/admin/tokens", dependencies=[Depends(require_admin)], status_code=201)
def create_token(body: TokenCreate):
    token = "pr_" + secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    row = query(
        "INSERT INTO tokens (name, token_hash, prefix) VALUES (?, ?, ?) RETURNING id",
        (body.name, digest, token[:10]),
    )[0]
    return {"id": row["id"], "name": body.name, "token": token}


@app.patch("/admin/tokens/{token_id}", dependencies=[Depends(require_admin)])
def update_token(token_id: int, body: TokenUpdate):
    rows = query("UPDATE tokens SET active = ? WHERE id = ? RETURNING id, active", (body.active, token_id))
    if not rows:
        raise HTTPException(404, "Token not found.")
    return rows[0]


@app.delete("/admin/tokens/{token_id}", dependencies=[Depends(require_admin)], status_code=204)
def delete_token(token_id: int):
    if not query("DELETE FROM tokens WHERE id = ? RETURNING id", (token_id,)):
        raise HTTPException(404, "Token not found.")


@app.post("/analyze", response_model=AnalyzeResponse, dependencies=[Depends(require_token)])
async def analyze(body: AnalyzeRequest):
    async with httpx.AsyncClient(timeout=60) as client:
        features = await collect_features(client, body.pr_url)
        analysis = await analyze_with_gemini(client, features)
    # Known outcomes should never depend on a model's prediction.
    if features["merged"]:
        analysis.merge_probability = 100
        analysis.merge_reasoning = "GitHub reports that this pull request is already merged."
    elif features["state"] == "closed":
        analysis.merge_probability = 0
        analysis.merge_reasoning = "This pull request is currently closed without merging."
    return AnalyzeResponse(pr_url=features["pr_url"], features=features, analysis=analysis)

