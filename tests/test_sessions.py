import base64
import hashlib
import hmac
import http.client
import json
import os
import tempfile
import threading
import unittest
from datetime import timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

# Keep tests isolated from the developer's real credentials and signing secret.
os.environ["AGENT_TIMELINE_USERNAME"] = "timeline-test-user"
os.environ["AGENT_TIMELINE_PASSWORD"] = "synthetic-test-password"
os.environ["AGENT_TIMELINE_SESSION_SECRET"] = "synthetic-test-session-secret"
TEST_STATE_DIR = tempfile.TemporaryDirectory(prefix="agent-timeline-session-tests-")
os.environ["AGENT_TIMELINE_SESSION_STATE"] = str(Path(TEST_STATE_DIR.name) / "state.json")

import server


class LoginInputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs = {}

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            values = dict(attrs)
            if "id" in values:
                self.inputs[values["id"]] = values


class SessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)
        TEST_STATE_DIR.cleanup()

    def setUp(self):
        server.SESSION_STATE_PATH = Path(TEST_STATE_DIR.name) / f"{self._testMethodName}.json"
        self.assertEqual(server.initialize_session_state(), 0)

    def request(self, method, path, body=None, cookie=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {}
        if body is not None:
            body = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        result = (response.status, dict(response.getheaders()), payload)
        connection.close()
        return result

    def login(self):
        status, headers, _ = self.request("POST", "/api/login", {
            "username": "timeline-test-user",
            "password": "synthetic-test-password",
        })
        self.assertEqual(status, 200)
        return headers["Set-Cookie"]

    def token_expiry(self, cookie):
        token = cookie.split(";", 1)[0].split("=", 1)[1]
        encoded = token.split(".", 1)[0]
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        return int(payload.decode().split("\n", 2)[1])

    def legacy_cookie(self, expires):
        payload = f"timeline-test-user\n{expires}\nlegacy-nonce".encode()
        signature = hmac.new(server.SESSION_SECRET.encode(), payload, hashlib.sha256).digest()
        token = server.b64(payload) + "." + server.b64(signature)
        return f"{server.COOKIE}={token}"

    def assert_persistent_cookie(self, cookie, max_age):
        parts = [part.strip() for part in cookie.split(";")]
        attrs = {part.partition("=")[0].lower(): part.partition("=")[2] for part in parts[1:]}
        flags = {part.lower() for part in parts[1:] if "=" not in part}
        self.assertEqual(attrs.get("path"), "/")
        self.assertEqual(attrs.get("max-age"), str(max_age))
        self.assertIn("expires", attrs)
        self.assertIn("httponly", flags)
        self.assertIn("secure", flags)
        self.assertEqual(attrs.get("samesite", "").lower(), "lax")
        return attrs

    def test_login_cookie_is_signed_secure_and_valid_for_400_days(self):
        cookie = self.login()
        attrs = self.assert_persistent_cookie(cookie, server.SESSION_SECONDS)
        self.assertTrue(server.valid_session(cookie.split(";", 1)[0].split("=", 1)[1]))
        expires = parsedate_to_datetime(attrs["expires"]).replace(tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(expires, self.token_expiry(cookie), delta=1)

    def test_existing_short_session_is_accepted_and_slid_to_400_days(self):
        old_expiry = int(server.time.time()) + 600
        old_cookie = self.legacy_cookie(old_expiry)
        status, headers, _ = self.request("GET", "/api/session", cookie=old_cookie)
        self.assertEqual(status, 200)
        renewed = headers["Set-Cookie"]
        self.assert_persistent_cookie(renewed, server.SESSION_SECONDS)
        self.assertGreater(self.token_expiry(renewed), old_expiry + 300 * 24 * 60 * 60)
        self.assertTrue(server.valid_session(renewed.split(";", 1)[0].split("=", 1)[1]))

    def test_logout_expires_cookie_and_session_endpoint_requires_login(self):
        cookie = self.login().split(";", 1)[0]
        another_device_cookie = self.login().split(";", 1)[0]
        old_format_cookie = self.legacy_cookie(int(server.time.time()) + 600)
        status, headers, _ = self.request("POST", "/api/logout", cookie=cookie.split(";", 1)[0])
        self.assertEqual(status, 200)
        self.assertIn("max-age=0", headers["Set-Cookie"].lower())
        self.assertIn("expires=thu, 01 jan 1970", headers["Set-Cookie"].lower())
        self.assertEqual(server.session_generation(), 1)
        self.assertEqual((server.SESSION_STATE_PATH.stat().st_mode & 0o777), 0o600)
        for revoked_cookie in (cookie, another_device_cookie, old_format_cookie):
            status, headers, _ = self.request("GET", "/api/session", cookie=revoked_cookie)
            self.assertEqual(status, 401)
            self.assertNotIn("Set-Cookie", headers)
            status, headers, _ = self.request("POST", "/api/logout", cookie=revoked_cookie)
            self.assertEqual(status, 401)
            self.assertEqual(headers.get("Set-Cookie"), server.expired_session_cookie())
        fresh_cookie = self.login()
        status, _, _ = self.request("GET", "/api/session", cookie=fresh_cookie.split(";", 1)[0])
        self.assertEqual(status, 200)

    def test_corrupt_session_state_fails_closed_with_http_error(self):
        cookie = self.login().split(";", 1)[0]
        server.SESSION_STATE_PATH.write_text("not-json", encoding="utf-8")
        status, headers, _ = self.request("GET", "/api/session", cookie=cookie)
        self.assertEqual(status, 503)
        self.assertNotIn("Set-Cookie", headers)

    def test_missing_state_after_initialization_fails_closed(self):
        cookie = self.login().split(";", 1)[0]
        server.SESSION_STATE_PATH.unlink()
        status, headers, _ = self.request("GET", "/api/session", cookie=cookie)
        self.assertEqual(status, 503)
        self.assertNotIn("Set-Cookie", headers)
        status, headers, _ = self.request("POST", "/api/logout", cookie=cookie)
        self.assertEqual(status, 503)
        self.assertNotIn("Set-Cookie", headers)
        status, headers, _ = self.request("POST", "/api/login", {
            "username": "timeline-test-user",
            "password": "synthetic-test-password",
        })
        self.assertEqual(status, 503)
        self.assertNotIn("Set-Cookie", headers)
        status, headers, _ = self.request("POST", "/api/logout", cookie=cookie)
        self.assertEqual(status, 503)
        self.assertNotIn("Set-Cookie", headers)
        status, headers, _ = self.request("POST", "/api/login", {
            "username": "timeline-test-user",
            "password": "synthetic-test-password",
        })
        self.assertEqual(status, 503)
        self.assertNotIn("Set-Cookie", headers)

    def test_failed_logout_keeps_current_cookie_and_does_not_claim_success(self):
        cookie = self.login().split(";", 1)[0]
        with patch("server.os.replace", side_effect=OSError("synthetic write failure")):
            status, headers, _ = self.request("POST", "/api/logout", cookie=cookie)
        self.assertEqual(status, 503)
        self.assertNotIn("Set-Cookie", headers)
        status, _, _ = self.request("GET", "/api/session", cookie=cookie)
        self.assertEqual(status, 200)

    def test_directory_sync_failure_after_replacement_completes_logout(self):
        cookie = self.login().split(";", 1)[0]
        with patch("server.os.fsync", side_effect=[None, OSError("synthetic directory sync failure")]):
            status, headers, _ = self.request("POST", "/api/logout", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("max-age=0", headers["Set-Cookie"].lower())
        self.assertEqual(server.session_generation(), 1)
        status, _, _ = self.request("GET", "/api/session", cookie=cookie)
        self.assertEqual(status, 401)

    def test_unauthenticated_and_health_responses_do_not_set_cookies(self):
        status, headers, _ = self.request("GET", "/api/session")
        self.assertEqual(status, 401)
        self.assertNotIn("Set-Cookie", headers)
        status, headers, _ = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertNotIn("Set-Cookie", headers)

    def test_invalid_or_expired_tokens_are_rejected(self):
        token = self.login().split(";", 1)[0].split("=", 1)[1]
        encoded, signature = token.split(".", 1)
        signature_bytes = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        signature_bytes = bytes([signature_bytes[0] ^ 1]) + signature_bytes[1:]
        self.assertFalse(server.valid_session(encoded + "." + server.b64(signature_bytes)))
        expired = server.sign_session("timeline-test-user", 1)
        self.assertFalse(server.valid_session(expired))

    def test_login_fields_are_recognized_by_password_managers(self):
        parser = LoginInputs()
        parser.feed((Path(__file__).parents[1] / "public" / "index.html").read_text())
        self.assertEqual(parser.inputs["username"].get("autocomplete"), "username")
        self.assertEqual(parser.inputs["password"].get("type"), "password")
        self.assertEqual(parser.inputs["password"].get("name"), "password")
        self.assertEqual(parser.inputs["password"].get("autocomplete"), "current-password")
