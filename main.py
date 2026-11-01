from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", APP_DIR / "support_mate.db"))
logger = logging.getLogger("support_mate")
ADMIN_SESSION_COOKIE = "support_mate_admin"
ADMIN_CSRF_COOKIE = "support_mate_csrf"
ADMIN_SESSION_SECONDS = 12 * 60 * 60
STOP_WORDS = {
    "a", "an", "and", "are", "can", "do", "does", "for", "how", "i", "in",
    "is", "it", "me", "my", "of", "on", "or", "please", "the", "to", "we",
    "what", "when", "where", "which", "who", "with", "you", "your",
}
SYNONYMS = {
    "delivery": "shipping",
    "deliver": "shipping",
    "ship": "shipping",
    "shipped": "shipping",
    "arrive": "shipping",
    "arrives": "shipping",
    "arrival": "shipping",
    "long": "days",
    "cost": "price",
    "costs": "price",
    "payment": "pay",
    "paid": "pay",
    "refund": "return",
    "refunded": "return",
    "exchange": "return",
    "exchanges": "return",
    "opening": "hours",
    "open": "hours",
    "close": "hours",
    "closes": "hours",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_db() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def database_connection() -> Iterator[sqlite3.Connection]:
    connection = connect_db()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with database_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name TEXT NOT NULL,
                access_token_hash TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'escalated', 'resolved')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id),
                role TEXT NOT NULL CHECK (role IN ('customer', 'assistant', 'agent')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id),
                customer_name TEXT NOT NULL,
                subject TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'in_progress', 'resolved')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conversation_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(conversations)")
        }
        if "access_token_hash" not in conversation_columns:
            connection.execute(
                "ALTER TABLE conversations ADD COLUMN access_token_hash "
                "TEXT NOT NULL DEFAULT ''"
            )
        if connection.execute(
            "SELECT COUNT(*) FROM knowledge_entries"
        ).fetchone()[0] == 0:
            now = utc_now()
            connection.executemany(
                "INSERT INTO knowledge_entries (question, answer, created_at) "
                "VALUES (?, ?, ?)",
                [
                    (
                        "What are your business hours?",
                        "Our support team is available Monday through Friday, "
                        "9:00 AM to 5:00 PM.",
                        now,
                    ),
                    (
                        "What is your return policy?",
                        "You can request a return within 30 days of delivery. "
                        "Items must be unused and returned with proof of purchase.",
                        now,
                    ),
                    (
                        "How long does shipping take?",
                        "Standard shipping usually takes 3–5 business days. "
                        "You will receive tracking information when your order ships.",
                        now,
                    ),
                ],
            )


def normalize_terms(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {SYNONYMS.get(word, word) for word in words if word not in STOP_WORDS}


def admin_configuration() -> tuple[str, str, str] | None:
    username = os.environ.get("ADMIN_USERNAME", "")
    password = os.environ.get("ADMIN_PASSWORD", "")
    secret = os.environ.get("SESSION_SECRET", "")
    if not username or len(password) < 12 or len(secret) < 32:
        return None
    return username, password, secret


def constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def create_admin_session(username: str, secret: str) -> str:
    payload = f"{username}:{int(time.time()) + ADMIN_SESSION_SECONDS}".encode()
    encoded_payload = base64.urlsafe_b64encode(payload).rstrip(b"=")
    signature = hmac.new(secret.encode(), encoded_payload, hashlib.sha256).hexdigest()
    return f"{encoded_payload.decode()}.{signature}"


def verify_admin_session(token: str | None, username: str, secret: str) -> bool:
    if not token or "." not in token:
        return False
    encoded_payload, signature = token.rsplit(".", 1)
    try:
        payload_bytes = encoded_payload.encode("ascii")
    except UnicodeEncodeError:
        return False
    expected_signature = hmac.new(
        secret.encode(), payload_bytes, hashlib.sha256
    ).hexdigest()
    if not constant_time_equal(signature, expected_signature):
        return False
    try:
        padding = "=" * (-len(encoded_payload) % 4)
        payload = base64.urlsafe_b64decode(encoded_payload + padding).decode()
        token_username, expires_at = payload.rsplit(":", 1)
        return (
            constant_time_equal(token_username, username)
            and int(expires_at) > int(time.time())
        )
    except (ValueError, UnicodeDecodeError):
        return False


def require_admin(request: Request) -> str:
    configuration = admin_configuration()
    if configuration is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Admin authentication is not configured. Set ADMIN_USERNAME, "
                "a 12-character ADMIN_PASSWORD, and a 32-character SESSION_SECRET."
            ),
        )
    username, _, secret = configuration
    if not verify_admin_session(
        request.cookies.get(ADMIN_SESSION_COOKIE), username, secret
    ):
        raise HTTPException(status_code=401, detail="Admin login required")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        csrf_cookie = request.cookies.get(ADMIN_CSRF_COOKIE, "")
        csrf_header = request.headers.get("X-CSRF-Token", "")
        if not csrf_cookie or not constant_time_equal(csrf_cookie, csrf_header):
            raise HTTPException(status_code=403, detail="CSRF token is missing or invalid")
    return username


def find_knowledge_entry(
    message: str, entries: list[sqlite3.Row]
) -> sqlite3.Row | None:
    query_terms = normalize_terms(message)
    if not query_terms:
        return None

    best_entry: sqlite3.Row | None = None
    best_score = 0.0
    for entry in entries:
        entry_terms = normalize_terms(f"{entry['question']} {entry['answer']}")
        overlap = query_terms & entry_terms
        score = len(overlap) / len(query_terms)
        if overlap and score > best_score:
            best_entry = entry
            best_score = score
    if best_entry is None or best_score < 0.25:
        return None
    best_terms = normalize_terms(f"{best_entry['question']} {best_entry['answer']}")
    overlap = query_terms & best_terms
    return best_entry if len(overlap) >= 2 or len(query_terms) == 1 else None


def generate_ai_answer(question: str, knowledge: str) -> str | None:
    api_key = os.environ.get("AI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None

    base_url = os.environ.get("AI_API_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    payload = json.dumps(
        {
            "model": os.environ.get("AI_MODEL", "gpt-4o-mini"),
            "temperature": 0.2,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a helpful customer support assistant. Answer the "
                        "customer only using the business information below. If it "
                        "does not contain the answer, reply exactly: "
                        "I don't have that information yet."
                        f"\n\nBusiness information:\n{knowledge}"
                    ),
                },
                {"role": "user", "content": question},
            ],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        answer = result["choices"][0]["message"]["content"]
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("AI API returned an empty or invalid answer")
        answer = answer.strip()
        if answer.lower() == "i don't have that information yet.":
            return None
        return answer
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError) as error:
        logger.exception("AI API request failed: %s", error)
        return None


class ChatRequest(BaseModel):
    message: Annotated[str, Field(min_length=1, max_length=4000)]
    conversation_id: int | None = Field(default=None, gt=0)
    customer_name: Annotated[str, Field(min_length=1, max_length=100)] = "Guest"
    conversation_token: Annotated[str | None, Field(max_length=128)] = None


class LoginRequest(BaseModel):
    username: Annotated[str, Field(min_length=1, max_length=100)]
    password: Annotated[str, Field(min_length=1, max_length=256)]


class KnowledgeRequest(BaseModel):
    question: Annotated[str, Field(min_length=3, max_length=500)]
    answer: Annotated[str, Field(min_length=3, max_length=5000)]


class TicketStatusRequest(BaseModel):
    status: str


class AgentReplyRequest(BaseModel):
    message: Annotated[str, Field(min_length=1, max_length=4000)]
    resolve: bool = False


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(title="Support Mate", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, bool]:
    return {
        "ok": True,
        "ai_enabled": bool(
            os.environ.get("AI_API_KEY") or os.environ.get("OPENAI_API_KEY")
        ),
    }


@app.get("/api/auth/session")
def admin_session(request: Request) -> dict[str, object]:
    configuration = admin_configuration()
    if configuration is None:
        return {"configured": False, "authenticated": False}
    username, _, secret = configuration
    authenticated = verify_admin_session(
        request.cookies.get(ADMIN_SESSION_COOKIE), username, secret
    )
    return {
        "configured": True,
        "authenticated": authenticated,
        "username": username if authenticated else None,
    }


@app.post("/api/auth/login")
def admin_login(payload: LoginRequest) -> JSONResponse:
    configuration = admin_configuration()
    if configuration is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Admin authentication is not configured. Set ADMIN_USERNAME, "
                "a 12-character ADMIN_PASSWORD, and a 32-character SESSION_SECRET."
            ),
        )
    username, password, secret = configuration
    username_matches = constant_time_equal(payload.username, username)
    password_matches = constant_time_equal(payload.password, password)
    if not (username_matches and password_matches):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    csrf_token = secrets.token_urlsafe(32)
    secure_cookie = (
        os.environ.get("APP_ENV", "").casefold() == "production"
        or os.environ.get("SESSION_COOKIE_SECURE", "").casefold() == "true"
    )
    response = {
        "authenticated": True,
        "username": username,
        "csrf_token": csrf_token,
    }
    json_response = JSONResponse(response)
    json_response.set_cookie(
        ADMIN_SESSION_COOKIE,
        create_admin_session(username, secret),
        max_age=ADMIN_SESSION_SECONDS,
        httponly=True,
        secure=secure_cookie,
        samesite="strict",
        path="/",
    )
    json_response.set_cookie(
        ADMIN_CSRF_COOKIE,
        csrf_token,
        max_age=ADMIN_SESSION_SECONDS,
        httponly=False,
        secure=secure_cookie,
        samesite="strict",
        path="/",
    )
    return json_response


@app.post("/api/auth/logout")
def admin_logout(request: Request) -> dict[str, bool]:
    csrf_cookie = request.cookies.get(ADMIN_CSRF_COOKIE, "")
    csrf_header = request.headers.get("X-CSRF-Token", "")
    if not csrf_cookie or not constant_time_equal(csrf_cookie, csrf_header):
        raise HTTPException(status_code=403, detail="CSRF token is missing or invalid")
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(ADMIN_SESSION_COOKIE, path="/", samesite="strict")
    response.delete_cookie(ADMIN_CSRF_COOKIE, path="/", samesite="strict")
    return response


@app.post("/api/chat")
def chat(payload: ChatRequest) -> dict[str, object]:
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="Message cannot be blank")

    with database_connection() as connection:
        now = utc_now()
        conversation_token = payload.conversation_token
        if payload.conversation_id is None:
            conversation_token = secrets.token_urlsafe(32)
            conversation = connection.execute(
                "INSERT INTO conversations "
                "(customer_name, access_token_hash, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    payload.customer_name.strip() or "Guest",
                    hashlib.sha256(conversation_token.encode()).hexdigest(),
                    now,
                    now,
                ),
            )
            conversation_id = conversation.lastrowid
        else:
            exists = connection.execute(
                "SELECT id, access_token_hash FROM conversations WHERE id = ?",
                (payload.conversation_id,),
            ).fetchone()
            if (
                exists is None
                or not conversation_token
                or not hmac.compare_digest(
                    exists["access_token_hash"],
                    hashlib.sha256(conversation_token.encode()).hexdigest(),
                )
            ):
                raise HTTPException(status_code=404, detail="Conversation not found")
            conversation_id = payload.conversation_id
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )

        connection.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) "
            "VALUES (?, 'customer', ?, ?)",
            (conversation_id, message, now),
        )
        entries = connection.execute(
            "SELECT id, question, answer FROM knowledge_entries"
        ).fetchall()
        match = find_knowledge_entry(message, entries)

        if match is not None:
            answer = generate_ai_answer(
                message, f"Question: {match['question']}\nAnswer: {match['answer']}"
            ) or match["answer"]
            escalated = False
            ticket_id = None
        else:
            answer = (
                "I don't have that information yet, so I've sent your question to "
                "our support team. A team member will follow up with you."
            )
            ticket = connection.execute(
                "INSERT INTO tickets "
                "(conversation_id, customer_name, subject, description, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    payload.customer_name.strip() or "Guest",
                    message[:100],
                    message,
                    now,
                    now,
                ),
            )
            ticket_id = ticket.lastrowid
            escalated = True
            connection.execute(
                "UPDATE conversations SET status = 'escalated' WHERE id = ?",
                (conversation_id,),
            )

        connection.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) "
            "VALUES (?, 'assistant', ?, ?)",
            (conversation_id, answer, utc_now()),
        )

    return {
        "conversation_id": conversation_id,
        "answer": answer,
        "escalated": escalated,
        "ticket_id": ticket_id,
        "conversation_token": conversation_token,
    }


@app.get("/api/conversations/{conversation_id}")
def get_conversation(
    conversation_id: int,
    conversation_token: Annotated[str | None, Header(alias="X-Conversation-Token")] = None,
) -> dict[str, object]:
    with database_connection() as connection:
        conversation = connection.execute(
            "SELECT id, customer_name, access_token_hash, status, created_at "
            "FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if (
            conversation is None
            or not conversation_token
            or not hmac.compare_digest(
                conversation["access_token_hash"],
                hashlib.sha256(conversation_token.encode()).hexdigest(),
            )
        ):
            raise HTTPException(status_code=404, detail="Conversation not found")
        messages = connection.execute(
            "SELECT id, role, content, created_at FROM messages "
            "WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
    return {
        "conversation": {
            key: conversation[key]
            for key in ("id", "customer_name", "status", "created_at")
        },
        "messages": [dict(row) for row in messages],
    }


@app.get("/api/dashboard")
def dashboard(_: str = Depends(require_admin)) -> dict[str, object]:
    with database_connection() as connection:
        stats = {
            "conversations": connection.execute(
                "SELECT COUNT(*) FROM conversations"
            ).fetchone()[0],
            "open_tickets": connection.execute(
                "SELECT COUNT(*) FROM tickets WHERE status != 'resolved'"
            ).fetchone()[0],
            "resolved_tickets": connection.execute(
                "SELECT COUNT(*) FROM tickets WHERE status = 'resolved'"
            ).fetchone()[0],
            "knowledge_articles": connection.execute(
                "SELECT COUNT(*) FROM knowledge_entries"
            ).fetchone()[0],
        }
        conversations = connection.execute(
            """
            SELECT c.id, c.customer_name, c.status, c.updated_at,
                   (SELECT m.content FROM messages m
                    WHERE m.conversation_id = c.id ORDER BY m.id DESC LIMIT 1) AS last_message
            FROM conversations c
            ORDER BY c.updated_at DESC LIMIT 8
            """
        ).fetchall()
        tickets = connection.execute(
            "SELECT id, conversation_id, customer_name, subject, description, "
            "status, created_at, updated_at FROM tickets "
            "ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END, "
            "updated_at DESC LIMIT 100"
        ).fetchall()
    return {
        "stats": stats,
        "conversations": [dict(row) for row in conversations],
        "tickets": [dict(row) for row in tickets],
    }


@app.get("/api/knowledge")
def list_knowledge(_: str = Depends(require_admin)) -> list[dict[str, object]]:
    with database_connection() as connection:
        entries = connection.execute(
            "SELECT id, question, answer, created_at FROM knowledge_entries ORDER BY id DESC"
        ).fetchall()
    return [dict(row) for row in entries]


@app.post("/api/knowledge", status_code=201)
def add_knowledge(
    payload: KnowledgeRequest, _: str = Depends(require_admin)
) -> dict[str, object]:
    with database_connection() as connection:
        result = connection.execute(
            "INSERT INTO knowledge_entries (question, answer, created_at) VALUES (?, ?, ?)",
            (payload.question.strip(), payload.answer.strip(), utc_now()),
        )
        entry = connection.execute(
            "SELECT id, question, answer, created_at FROM knowledge_entries WHERE id = ?",
            (result.lastrowid,),
        ).fetchone()
    return dict(entry)


@app.delete("/api/knowledge/{entry_id}", status_code=204)
def delete_knowledge(
    entry_id: int, _: str = Depends(require_admin)
) -> None:
    with database_connection() as connection:
        result = connection.execute(
            "DELETE FROM knowledge_entries WHERE id = ?", (entry_id,)
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Knowledge article not found")


@app.patch("/api/tickets/{ticket_id}")
def update_ticket(
    ticket_id: int,
    payload: TicketStatusRequest,
    _: str = Depends(require_admin),
) -> dict[str, object]:
    if payload.status not in {"open", "in_progress", "resolved"}:
        raise HTTPException(
            status_code=422,
            detail="Status must be open, in_progress, or resolved",
        )
    with database_connection() as connection:
        result = connection.execute(
            "UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?",
            (payload.status, utc_now(), ticket_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Ticket not found")
        if payload.status == "resolved":
            connection.execute(
                "UPDATE conversations SET status = 'resolved', updated_at = ? "
                "WHERE id = (SELECT conversation_id FROM tickets WHERE id = ?)",
                (utc_now(), ticket_id),
            )
        elif payload.status == "in_progress":
            connection.execute(
                "UPDATE conversations SET status = 'escalated', updated_at = ? "
                "WHERE id = (SELECT conversation_id FROM tickets WHERE id = ?)",
                (utc_now(), ticket_id),
            )
        row = connection.execute(
            "SELECT id, status, updated_at FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
    return dict(row)


@app.post("/api/tickets/{ticket_id}/reply")
def reply_to_ticket(
    ticket_id: int,
    payload: AgentReplyRequest,
    _: str = Depends(require_admin),
) -> dict[str, object]:
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="Reply cannot be blank")
    now = utc_now()
    with database_connection() as connection:
        ticket = connection.execute(
            "SELECT conversation_id FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if ticket is None:
            raise HTTPException(status_code=404, detail="Ticket not found")
        conversation_id = ticket["conversation_id"]
        connection.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) "
            "VALUES (?, 'agent', ?, ?)",
            (conversation_id, message, now),
        )
        status = "resolved" if payload.resolve else "in_progress"
        connection.execute(
            "UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, ticket_id),
        )
        connection.execute(
            "UPDATE conversations SET status = ?, updated_at = ? WHERE id = ?",
            ("resolved" if payload.resolve else "escalated", now, conversation_id),
        )
    return {"ticket_id": ticket_id, "status": status, "conversation_id": conversation_id}
