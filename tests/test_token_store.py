import json
import os
import threading
import time
import unittest
from unittest import mock

import requests

import token_store
from tests.support import AppTestCase, FakeResponse, app_module

SECRET = {"access_token": "SUPER-SECRET-ACCESS", "refresh_token": "SUPER-SECRET-REFRESH", "tenant_id": "t1"}


class TokenStoreTests(AppTestCase):
    def path(self):
        return os.path.join(self.tmp, "tokens.json")

    def raw(self):
        with open(self.path(), "rb") as fh:
            return fh.read()

    def test_plaintext_mode_keeps_original_format_and_owner_only_permissions(self):
        token_store.save(self.path(), SECRET)
        self.assertEqual(json.loads(self.raw()), SECRET)
        self.assertEqual(token_store.load(self.path()), SECRET)
        if os.name == "posix":
            self.assertEqual(os.stat(self.path()).st_mode & 0o777, 0o600)

    def test_encrypted_at_rest_and_persists_across_reads(self):
        os.environ["TOKEN_ENCRYPTION_KEY"] = "any passphrase works"
        token_store.save(self.path(), SECRET)
        self.assertTrue(self.raw().startswith(b"enc:v1:"))
        self.assertNotIn(b"SUPER-SECRET", self.raw())
        self.assertEqual(token_store.load(self.path()), SECRET)  # "restart": nothing cached in memory

    def test_legacy_plaintext_file_is_migrated_without_reconnect(self):
        token_store.save(self.path(), SECRET)  # plaintext (no key yet)
        os.environ["TOKEN_ENCRYPTION_KEY"] = "k"
        self.assertEqual(token_store.load(self.path()), SECRET)
        self.assertTrue(self.raw().startswith(b"enc:v1:"))
        self.assertNotIn(b"SUPER-SECRET", self.raw())

    def test_wrong_or_missing_key_raises_and_never_modifies_the_file(self):
        os.environ["TOKEN_ENCRYPTION_KEY"] = "key-A"
        token_store.save(self.path(), SECRET)
        before = self.raw()
        os.environ["TOKEN_ENCRYPTION_KEY"] = "key-B"
        with self.assertRaises(token_store.TokenStoreError) as ctx:
            token_store.load(self.path())
        self.assertNotIn("SUPER-SECRET", str(ctx.exception))
        del os.environ["TOKEN_ENCRYPTION_KEY"]
        with self.assertRaises(token_store.TokenStoreError):
            token_store.load(self.path())
        self.assertEqual(self.raw(), before)

    def test_tampered_ciphertext_and_corrupt_plaintext_are_rejected(self):
        os.environ["TOKEN_ENCRYPTION_KEY"] = "k"
        token_store.save(self.path(), SECRET)
        data = bytearray(self.raw())
        data[-5] = ord("A") if data[-5] != ord("A") else ord("B")
        with open(self.path(), "wb") as fh:
            fh.write(bytes(data))
        with self.assertRaises(token_store.TokenStoreError):
            token_store.load(self.path())
        del os.environ["TOKEN_ENCRYPTION_KEY"]
        with open(self.path(), "w") as fh:
            fh.write("{not json")
        with self.assertRaises(token_store.TokenStoreError):
            token_store.load(self.path())

    def test_missing_and_empty_files_return_none_and_no_temp_files_are_left(self):
        self.assertIsNone(token_store.load(self.path()))
        token_store.save(self.path(), SECRET)
        token_store.save(self.path(), SECRET)
        self.assertTrue(set(os.listdir(self.tmp)) <= {"tokens.json", "tokens.json.lock"})
        open(self.path(), "w").close()
        self.assertIsNone(token_store.load(self.path()))

    def test_reentrant_lock_does_not_deadlock(self):
        with token_store.locked(self.path()):
            with token_store.locked(self.path()):
                token_store.save(self.path(), SECRET)
        self.assertEqual(token_store.load(self.path()), SECRET)

    def test_app_helpers_use_the_store_and_encrypt_when_a_key_is_set(self):
        os.environ["TOKEN_ENCRYPTION_KEY"] = "k"
        app_module.save_token({"access_token": "AT-x", "refresh_token": "RT-x", "expires_in": 1800},
                              [{"tenantId": "t9", "tenantName": "Nine"}])
        with open(app_module.TOKEN_FILE, "rb") as fh:
            self.assertNotIn(b"AT-x", fh.read())
        self.assertEqual(app_module.load_token()["tenant_name"], "Nine")

    def test_concurrent_requests_refresh_exactly_once(self):
        self.write_token(created_at=time.time() - 5000)  # expired
        calls = []

        def slow_post(url, data=None, **kw):
            calls.append(data["refresh_token"])
            time.sleep(0.1)
            return FakeResponse(200, {"access_token": "AT-2", "refresh_token": "RT-2", "expires_in": 1800})

        results = []
        with mock.patch.object(requests, "post", side_effect=slow_post):
            threads = [threading.Thread(target=lambda: results.append(app_module.get_valid_token()["access_token"])) for _ in range(6)]
            [t.start() for t in threads]
            [t.join() for t in threads]
        self.assertEqual(calls, ["RT-secret-1"], "the single-use refresh token must be spent exactly once")
        self.assertEqual(results, ["AT-2"] * 6)
        self.assertEqual(app_module.load_token()["refresh_token"], "RT-2")

    def test_unreadable_token_file_gives_clear_error_not_a_crash(self):
        os.environ["TOKEN_ENCRYPTION_KEY"] = "key-A"
        token_store.save(app_module.TOKEN_FILE, SECRET)
        os.environ["TOKEN_ENCRYPTION_KEY"] = "key-B"
        r = self.client.get("/")
        self.assertEqual(r.status_code, 500)
        self.assertIn("TOKEN_ENCRYPTION_KEY", r.get_json()["error"])
        self.assertNotIn("SUPER-SECRET", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
