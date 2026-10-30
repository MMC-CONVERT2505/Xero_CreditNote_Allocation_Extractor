"""Shared helpers for the test-suite (stdlib unittest only - no new dependencies)."""
import copy
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

from requests.structures import CaseInsensitiveDict

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "golden")

# Must be set before `app` is imported (app.py reads them at import time).
os.environ.setdefault("CLIENT_ID", "test-client-id")
os.environ.setdefault("CLIENT_SECRET", "test-client-secret")
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef"
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import app as app_module  # noqa: E402

FEATURE_ENV = ("ENABLE_ALLOCATION_REMOVAL", "TOKEN_ENCRYPTION_KEY", "REMOVAL_ALLOWED_TENANT_IDS",
               "ALLOCATION_AUDIT_LOG", "XERO_REDIRECT_URI", "XERO_SCOPES", "XERO_TOKEN_FILE")


class FakeResponse:
    def __init__(self, status=200, json_data=None, headers=None, text=None):
        self.status_code = status
        self._json = json_data
        self.headers = CaseInsensitiveDict(headers or {})
        self.text = text if text is not None else (json.dumps(json_data) if json_data is not None else "")
        self.content = self.text.encode()
        self.ok = status < 400

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return copy.deepcopy(self._json)


class AppTestCase(unittest.TestCase):
    """Runs every test inside a throw-away working directory (token file / audit log live there)."""

    def setUp(self):
        self._cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp(prefix="xat-")
        os.chdir(self.tmp)
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for k in FEATURE_ENV:
            os.environ.pop(k, None)
        self.client = app_module.app.test_client()

    def tearDown(self):
        self._env.stop()
        os.chdir(self._cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_token(self, **overrides):
        tok = {"access_token": "AT-secret-1", "refresh_token": "RT-secret-1", "expires_in": 1800,
               "created_at": time.time(), "tenant_id": "tenant-1", "tenant_name": "Demo Co",
               "token_type": "Bearer"}
        tok.update(overrides)
        with open(app_module.TOKEN_FILE, "w") as f:
            json.dump(tok, f, indent=2)
        return tok
