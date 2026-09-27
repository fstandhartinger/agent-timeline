#!/usr/bin/env python3
"""Authenticated API for the Agent Timeline's read-only activity view."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import tempfile
import threading
import time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

DB_PATH = Path(os.path.expanduser(os.environ.get("AGENT_TIMELINE_DB", "~/.local/share/agent-timeline/agents.sqlite3")))
USERNAME = os.environ.get("AGENT_TIMELINE_USERNAME", "admin")
PASSWORD = os.environ.get("AGENT_TIMELINE_PASSWORD", "")
SESSION_SECRET = os.environ.get("AGENT_TIMELINE_SESSION_SECRET", "")
HOST = os.environ.get("AGENT_TIMELINE_HOST", "127.0.0.1")
PORT = int(os.environ.get("AGENT_TIMELINE_PORT", "8890"))
COOKIE = "agent_timeline_session"
SESSION_SECONDS = 400 * 24 * 60 * 60
LEGACY_SESSION_SECONDS = 10 * 60 * 60
SESSION_STATE_PATH = Path(os.path.expanduser(os.environ.get(
    "AGENT_TIMELINE_SESSION_STATE", str(DB_PATH.with_name("session-state.json")))))
SESSION_STATE_LOCK = threading.RLock()
LOGIN_WINDOW = 15 * 60
LOGIN_MAX = 7
MAX_HISTORY_SECONDS = 50 * 365 * 86400
ATTEMPTS: dict[str, list[int]] = {}

def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def sign_session(username: str, expires: int) -> str:
    with SESSION_STATE_LOCK:
        nonce = secrets.token_urlsafe(10)
        generation = session_generation()
        payload = f"{username}\n{expires}\n{nonce}\n{generation}".encode()
        signature = hmac.new(SESSION_SECRET.encode(), payload, hashlib.sha256).digest()
    return b64(payload) + "." + b64(signature)


def valid_session(token: str) -> bool:
    try:
        encoded, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        signature = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
        expected = hmac.new(SESSION_SECRET.encode(), payload, hashlib.sha256).digest()
        fields = payload.decode("utf-8").split("\n")
        if len(fields) == 3:
            user, expires, _ = fields
            generation = 0
            issued_at_ns = (int(expires) - LEGACY_SESSION_SECONDS) * 1_000_000_000
        elif len(fields) == 4:
            user, expires, _, encoded_generation = fields
            encoded_value = int(encoded_generation)
            if encoded_value >= 1_000_000_000_000:
                # Earlier deployments used an issuance timestamp as the fourth field.
                generation = 0
                issued_at_ns = encoded_value
            else:
                generation = encoded_value
                issued_at_ns = None
        else:
            return False
        expires_at = int(expires)
        valid_signature = hmac.compare_digest(signature, expected)
    except Exception:
        return False
    if not valid_signature or user != USERNAME or expires_at <= int(time.time()):
        return False
    state = read_session_state()
    return (generation == state["generation"]
            and (issued_at_ns is None or issued_at_ns > state["legacy_invalid_before_ns"]))


def read_session_state() -> dict[str, int]:
    with SESSION_STATE_LOCK:
        state = json.loads(SESSION_STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("invalid session state")
        generation = state.get("generation")
        legacy_invalid_before_ns = state.get("legacy_invalid_before_ns", 0)
        if "invalid_before_ns" in state:
            raise ValueError("session state requires migration")
        if (not isinstance(generation, int) or isinstance(generation, bool) or generation < 0
                or not isinstance(legacy_invalid_before_ns, int)
                or isinstance(legacy_invalid_before_ns, bool) or legacy_invalid_before_ns < 0):
            raise ValueError("invalid session state")
        return {"generation": generation, "legacy_invalid_before_ns": legacy_invalid_before_ns}


def session_generation() -> int:
    return read_session_state()["generation"]


def store_session_state(generation: int, legacy_invalid_before_ns: int) -> None:
    SESSION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=".session-state-", dir=SESSION_STATE_PATH.parent)
    try:
        os.fchmod(fd, 0o600)
        state_file = os.fdopen(fd, "w", encoding="utf-8")
        fd = None
        with state_file:
            json.dump({
                "generation": generation,
                "legacy_invalid_before_ns": legacy_invalid_before_ns,
            }, state_file)
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary_name, SESSION_STATE_PATH)
        directory_fd = os.open(SESSION_STATE_PATH.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def initialize_session_state() -> int:
    with SESSION_STATE_LOCK:
        try:
            state = json.loads(SESSION_STATE_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            store_session_state(0, 0)
            return 0
        if isinstance(state, dict) and "invalid_before_ns" in state and "generation" not in state:
            legacy_invalid_before_ns = state.get("invalid_before_ns")
            if (not isinstance(legacy_invalid_before_ns, int)
                    or isinstance(legacy_invalid_before_ns, bool) or legacy_invalid_before_ns < 0):
                raise ValueError("invalid legacy session state")
            store_session_state(0, legacy_invalid_before_ns)
            return 0
        return read_session_state()["generation"]


def invalidate_sessions() -> None:
    with SESSION_STATE_LOCK:
        state = read_session_state()
        store_session_state(state["generation"] + 1, state["legacy_invalid_before_ns"])


def session_cookie(token: str, expires: int) -> str:
    return (f"{COOKIE}={token}; Path=/; Max-Age={SESSION_SECONDS}; "
            f"Expires={formatdate(expires, usegmt=True)}; HttpOnly; Secure; SameSite=Lax")


def expired_session_cookie() -> str:
    return (f"{COOKIE}=; Path=/; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; "
            "HttpOnly; Secure; SameSite=Lax")


def db_read() -> sqlite3.Connection:
    uri = "file:" + quote(str(DB_PATH), safe="/:.") + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA busy_timeout=15000")
    return db


def client_ip(handler: BaseHTTPRequestHandler) -> str:
    forwarded = handler.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()[:64]
    return str(handler.client_address[0])[:64]


def recent_attempts(key: str) -> list[int]:
    now = int(time.time())
    attempts = [t for t in ATTEMPTS.get(key, []) if now - t < LOGIN_WINDOW]
    ATTEMPTS[key] = attempts
    return attempts


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentTimeline/1.0"
    sys_version = ""

    def log_message(self, *_args):
        return

    def send_json(self, status: int, obj: dict, headers: dict[str, str] | None = None):
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        refresh_cookie = getattr(self, "_refresh_cookie", None)
        if refresh_cookie:
            self.send_header("Set-Cookie", refresh_cookie)
        if headers:
            for name, value in headers.items(): self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def token(self) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == COOKIE: return value
        return ""

    def authorized(self) -> bool:
        return valid_session(self.token())

    def authorized_and_refresh(self) -> bool:
        with SESSION_STATE_LOCK:
            if not self.authorized():
                return False
            self._refresh_cookie = self.issue_session_cookie()
            return True

    def issue_session_cookie(self) -> str:
        expires = int(time.time()) + SESSION_SECONDS
        return session_cookie(sign_session(USERNAME, expires), expires)

    def do_GET(self):
        self._refresh_cookie = None
        path = urlparse(self.path).path
        if path == "/healthz":
            return self.send_json(200, {"ok": True})
        if path == "/api/session":
            try:
                authorized = self.authorized_and_refresh()
            except (OSError, ValueError):
                return self.send_json(503, {"error": "session service is temporarily unavailable"})
            if not authorized: return self.send_json(401, {"error": "login required"})
            return self.send_json(200, {"ok": True, "user": USERNAME})
        try:
            authorized = self.authorized_and_refresh()
        except (OSError, ValueError):
            return self.send_json(503, {"error": "session service is temporarily unavailable"})
        if not authorized: return self.send_json(401, {"error": "login required"})
        if path == "/api/stats":
            return self.stats()
        if path == "/api/agents":
            return self.agents()
        return self.send_json(404, {"error": "not found"})

    def do_POST(self):
        self._refresh_cookie = None
        path = urlparse(self.path).path
        if path == "/api/login":
            return self.login()
        if path == "/api/logout":
            with SESSION_STATE_LOCK:
                try:
                    authorized = self.authorized()
                except (OSError, ValueError):
                    return self.send_json(503, {"error": "session service is temporarily unavailable"})
                if not authorized:
                    return self.send_json(401, {"error": "login required"}, {
                        "Set-Cookie": expired_session_cookie()})
                try:
                    invalidate_sessions()
                except (OSError, ValueError):
                    return self.send_json(503, {"error": "logout could not be completed"})
            return self.send_json(200, {"ok": True}, {"Set-Cookie": expired_session_cookie()})
        return self.send_json(404, {"error": "not found"})

    def login(self):
        ip = client_ip(self)
        key = hashlib.sha256(ip.encode()).hexdigest()
        attempts = recent_attempts(key)
        if len(attempts) >= LOGIN_MAX:
            wait = max(1, LOGIN_WINDOW - (int(time.time()) - attempts[0]))
            return self.send_json(429, {"error": "Too many attempts. Try again later."}, {"Retry-After": str(wait)})
        try: length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError: return self.send_json(400, {"error": "bad request"})
        if length > 4096:
            self.close_connection = True
            return self.send_json(413, {"error": "request too large"})
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            user = str(data.get("username", ""))[:128]
            password = str(data.get("password", ""))[:512]
        except Exception:
            user, password = "", ""
        user_ok = hmac.compare_digest(user, USERNAME)
        pass_ok = hmac.compare_digest(password, PASSWORD)
        if user_ok and pass_ok:
            ATTEMPTS.pop(key, None)
            try:
                expires = int(time.time()) + SESSION_SECONDS
                token = sign_session(USERNAME, expires)
            except (OSError, ValueError):
                return self.send_json(503, {"error": "session service is temporarily unavailable"})
            return self.send_json(200, {"ok": True, "user": USERNAME}, {
                "Set-Cookie": session_cookie(token, expires)})
        attempts.append(int(time.time()))
        ATTEMPTS[key] = attempts
        return self.send_json(401, {"error": "Incorrect username or password"})

    def stats(self):
        try:
            db = db_read()
            rows = visible_rows(db, 0, int(time.time()) + 86400)
            total = len(rows)
            parents = sum(1 for r in rows if r["parent_id"])
            bounds = (min((r["start_at"] for r in rows), default=None),
                      max((r["end_at"] or r["last_seen"] for r in rows), default=None))
            topics = {}
            for row in rows: topics[row["topic"]] = topics.get(row["topic"], 0) + 1
            row = db.execute("SELECT value FROM kv WHERE key='last_collect'").fetchone()
            db.close()
            return self.send_json(200, {"agents": total, "with_parent": parents,
                "earliest": bounds[0], "latest": bounds[1], "collected_at": int(row[0]) if row else None,
                "topics": topics})
        except sqlite3.Error:
            return self.send_json(503, {"error": "timeline data is not ready"})

    def agents(self):
        query = parse_qs(urlparse(self.path).query)
        try:
            lower = max(0, int(query.get("from", [str(int(time.time()) - 28 * 86400)])[0]))
            upper = min(int(time.time()) + 86400, int(query.get("to", [str(int(time.time()))])[0]))
        except ValueError:
            return self.send_json(400, {"error": "invalid time range"})
        if upper <= lower or upper - lower > MAX_HISTORY_SECONDS:
            return self.send_json(400, {"error": "time range is too large"})
        try:
            db = db_read()
            rows = visible_rows(db, lower, upper)
            db.close()
            agents = [{"id": r["id"], "parent_id": r["parent_id"], "name": r["name"],
                "engine": r["engine"], "model": r["model"], "start": r["start_at"],
                "end": r["end_at"], "topic": r["topic"], "job_dir": r["job_dir"],
                "unit_name": r["unit_name"], "source": r["source"],
                "board_url": r["board_url"], "last_seen": r["last_seen"]} for r in rows]
            return self.send_json(200, {"agents": agents, "from": lower, "to": upper,
                "now": int(time.time()), "limit": 12000})
        except sqlite3.Error:
            return self.send_json(503, {"error": "timeline data is not ready"})


def visible_rows(db: sqlite3.Connection, lower: int, upper: int):
    rows = db.execute("""SELECT id,parent_id,name,engine,model,start_at,end_at,topic,
      job_dir,unit_name,source,board_url,last_seen,external_id
      FROM agents WHERE start_at<=? AND (end_at IS NULL OR end_at>=?)
      ORDER BY topic,start_at LIMIT 30000""", (upper, lower)).fetchall()
    session_sources = ("codex","claude","opencode","hermes","hermes_cron","cron","process")
    transcript_jobs = {r["job_dir"] for r in rows if r["job_dir"] and r["source"] in session_sources}
    observed_jobs = {r["job_dir"] for r in rows if r["job_dir"] and r["source"] != "job"}
    transcript_ids = {r["external_id"] for r in rows if r["external_id"] and r["source"] in ("codex","claude","opencode","hermes")}
    out = []
    for row in rows:
        if row["source"] == "job" and row["job_dir"] in observed_jobs:
            continue
        if row["source"] == "systemd" and row["job_dir"] in transcript_jobs:
            continue
        if row["source"] == "process" and row["external_id"] in transcript_ids:
            continue
        out.append(row)
        if len(out) >= 12000: break
    return out


def main(argv: list[str] | None = None):
    import sys

    args = sys.argv[1:] if argv is None else argv
    if args == ["--initialize-session-state"]:
        try:
            initialize_session_state()
        except (OSError, ValueError):
            raise SystemExit(
                "Agent Timeline session state cannot be initialized; restore a valid state or "
                "rotate its signing secret before creating a fresh one"
            ) from None
        return
    if args:
        raise SystemExit("Unknown Agent Timeline server option")
    if not PASSWORD or not SESSION_SECRET:
        raise SystemExit("Agent Timeline requires its local environment file")
    try:
        session_generation()
    except (OSError, ValueError):
        raise SystemExit("Agent Timeline session state is unavailable; restore it before starting")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__": main()
