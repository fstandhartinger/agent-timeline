#!/usr/bin/env python3
"""Small read-only API for the Agent Timeline; data is only served after login."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
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
SESSION_SECONDS = 10 * 60 * 60
LOGIN_WINDOW = 15 * 60
LOGIN_MAX = 7
MAX_HISTORY_SECONDS = 50 * 365 * 86400
ATTEMPTS: dict[str, list[int]] = {}

if not PASSWORD or not SESSION_SECRET:
    raise SystemExit("Agent Timeline requires its local environment file")


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def sign_session(username: str, expires: int) -> str:
    nonce = secrets.token_urlsafe(10)
    payload = f"{username}\n{expires}\n{nonce}".encode()
    signature = hmac.new(SESSION_SECRET.encode(), payload, hashlib.sha256).digest()
    return b64(payload) + "." + b64(signature)


def valid_session(token: str) -> bool:
    try:
        encoded, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        signature = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
        expected = hmac.new(SESSION_SECRET.encode(), payload, hashlib.sha256).digest()
        user, expires, _ = payload.decode("utf-8").split("\n", 2)
        return hmac.compare_digest(signature, expected) and user == USERNAME and int(expires) > int(time.time())
    except Exception:
        return False


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

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self.send_json(200, {"ok": True})
        if path == "/api/session":
            if not self.authorized(): return self.send_json(401, {"error": "login required"})
            return self.send_json(200, {"ok": True, "user": USERNAME})
        if not self.authorized(): return self.send_json(401, {"error": "login required"})
        if path == "/api/stats":
            return self.stats()
        if path == "/api/agents":
            return self.agents()
        return self.send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/login":
            return self.login()
        if path == "/api/logout":
            if not self.authorized(): return self.send_json(401, {"error": "login required"})
            return self.send_json(200, {"ok": True}, {
                "Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict"})
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
            expires = int(time.time()) + SESSION_SECONDS
            token = sign_session(USERNAME, expires)
            return self.send_json(200, {"ok": True, "user": USERNAME}, {
                "Set-Cookie": f"{COOKIE}={token}; Path=/; Max-Age={SESSION_SECONDS}; HttpOnly; Secure; SameSite=Strict"})
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


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__": main()
