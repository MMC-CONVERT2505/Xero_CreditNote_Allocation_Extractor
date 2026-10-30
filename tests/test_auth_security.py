import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

import requests

from tests.support import AppTestCase, FakeResponse, app_module
from tests.test_baseline import fake_get


def state_from(response):
    return parse_qs(urlparse(response.headers["Location"]).query)["state"][0]


class OAuthSecurityTests(AppTestCase):
    def callback(self, query):
        token = {"access_token": "AT-new", "refresh_token": "RT-new", "expires_in": 1800}
        with mock.patch.object(requests, "post", return_value=FakeResponse(200, token)) as post, \
                mock.patch.object(requests, "get", side_effect=fake_get):
            return self.client.get("/callback?" + query), post

    def test_state_is_random_per_login_and_not_the_old_constant(self):
        a, b = state_from(self.client.get("/auth")), state_from(self.client.get("/auth"))
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, "123456")
        self.assertGreaterEqual(len(a), 20)

    def test_wrong_missing_or_absent_state_is_rejected_before_any_token_exchange(self):
        state = state_from(self.client.get("/auth"))
        for query in ("code=abc&state=wrong", "code=abc", f"code=abc&state={state}x"):
            r, post = self.callback(query)
            self.assertEqual(r.status_code, 400, query)
            self.assertIn("invalid OAuth state", r.get_data(as_text=True))
            post.assert_not_called()
        # a browser that never started a login (no session) cannot complete a callback either
        r, post = self.callback("code=abc&state=123456")
        self.assertEqual(r.status_code, 400)
        post.assert_not_called()

    def test_state_is_single_use(self):
        state = state_from(self.client.get("/auth"))
        ok, _ = self.callback(f"code=abc&state={state}")
        self.assertEqual(ok.status_code, 200)
        replay, post = self.callback(f"code=abc&state={state}")
        self.assertEqual(replay.status_code, 400)
        post.assert_not_called()

    def test_tokens_never_live_in_the_session_cookie(self):
        state = state_from(self.client.get("/auth"))
        self.callback(f"code=abc&state={state}")
        with self.client.session_transaction() as s:
            self.assertNotIn("token", s)
            self.assertNotIn("AT-new", str(dict(s)))
        raw_cookies = " ".join(c.value for c in self.client._cookies.values()) if hasattr(self.client, "_cookies") else ""
        self.assertNotIn("AT-new", raw_cookies)

    def test_refresh_does_not_put_tokens_in_the_session(self):
        import time
        self.write_token(created_at=time.time() - 5000)
        new = {"access_token": "AT-2", "refresh_token": "RT-2", "expires_in": 1800}
        with mock.patch.object(requests, "post", return_value=FakeResponse(200, new)), \
                mock.patch.object(requests, "get", side_effect=fake_get):
            self.client.get("/extract/download/all?start=2025-01-01&end=2025-01-31")
        with self.client.session_transaction() as s:
            self.assertNotIn("token", s)

    def test_session_cookie_is_httponly_and_samesite_lax(self):
        cookie = self.client.get("/auth").headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_default_scopes_and_redirect_uri_are_unchanged(self):
        self.assertEqual(app_module.XERO_SCOPES, "offline_access accounting.transactions accounting.contacts")
        self.assertEqual(app_module.REDIRECT_URI, "http://localhost:5000/callback")


if __name__ == "__main__":
    unittest.main()
