"""
BASELINE / characterization tests for the ORIGINAL fetching tool.

They were written and run against the unmodified code first, and must keep passing unchanged after the
Allocation Removal feature is added (with the feature flag off, i.e. the default).
Set UPDATE_GOLDEN=1 to (re)create the golden HTML files (only do this on pristine code).
"""
import io
import json
import os
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

import openpyxl
import requests

from tests.support import GOLDEN_DIR, AppTestCase, FakeResponse, app_module

CREDIT_NOTES = {"CreditNotes": [
    {"CreditNoteID": "cn-1", "CreditNoteNumber": "CN-001", "Type": "ACCRECCREDIT", "Date": "/Date(1735689600000+0000)/",
     "Allocations": [{"AppliedAmount": 100.0, "Date": "/Date(1735776000000+0000)/",
                      "Invoice": {"InvoiceNumber": "INV-1", "Type": "ACCREC"}}]},
    {"CreditNoteID": "cn-2", "Reference": "SUP-REF", "Type": "ACCPAYCREDIT", "Date": "2025-01-05",
     "Allocations": [{"Amount": 75.5, "Date": "2025-01-06", "Invoice": {"InvoiceNumber": "BILL-9", "Type": "ACCPAY"}}]},
]}
OVERPAYMENTS = {"Overpayments": [
    {"OverpaymentID": "op-1", "Type": "RECEIVE-OVERPAYMENT", "Date": "2025-01-10",
     "Allocations": [{"Amount": 40.0, "Invoice": {"InvoiceNumber": "INV-7"}}]},
    {"OverpaymentID": "op-2", "Type": "SPEND-OVERPAYMENT", "Date": "2025-01-11",
     "Allocations": [{"Amount": 12.0, "Invoice": {"InvoiceNumber": "BILL-3"}}]},
]}


def fake_get(url, headers=None, **kw):
    if "/CreditNotes" in url:
        return FakeResponse(200, CREDIT_NOTES)
    if "/Overpayments" in url:
        return FakeResponse(200, OVERPAYMENTS)
    if url.endswith("/connections"):
        return FakeResponse(200, [{"tenantId": "tenant-1", "tenantName": "Demo Co"},
                                  {"tenantId": "tenant-2", "tenantName": "Second Co"}])
    return FakeResponse(404, {})


def sheets(resp):
    wb = openpyxl.load_workbook(io.BytesIO(resp.data))
    out = {}
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        out[ws.title] = [dict(zip(rows[0], r)) for r in rows[1:]] if rows else []
    return out


def golden(testcase, name, html):
    path = os.path.join(GOLDEN_DIR, name)
    if os.environ.get("UPDATE_GOLDEN") == "1":
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(html)
    with open(path, "r", encoding="utf-8", newline="") as f:
        testcase.assertEqual(html, f.read(), f"home page HTML changed vs golden {name}")


class BaselineTests(AppTestCase):
    def test_home_disconnected_matches_golden(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Not connected to Xero", r.get_data(as_text=True))
        golden(self, "home_disconnected.html", r.get_data(as_text=True))

    def test_home_connected_matches_golden(self):
        self.write_token()
        r = self.client.get("/")
        html = r.get_data(as_text=True)
        self.assertIn("Connected Organisation:</strong> Demo Co", html)
        golden(self, "home_connected.html", html)

    def test_auth_redirects_to_xero_with_expected_params(self):
        r = self.client.get("/auth")
        self.assertEqual(r.status_code, 302)
        loc = urlparse(r.headers["Location"])
        self.assertEqual(f"{loc.scheme}://{loc.netloc}{loc.path}", "https://login.xero.com/identity/connect/authorize")
        q = parse_qs(loc.query)
        self.assertEqual(q["client_id"], ["test-client-id"])
        self.assertEqual(q["redirect_uri"], ["http://localhost:5000/callback"])
        self.assertEqual(q["scope"], ["offline_access accounting.transactions accounting.contacts"])
        self.assertTrue(q["state"][0])

    def _connect(self):
        auth = self.client.get("/auth")
        state = parse_qs(urlparse(auth.headers["Location"]).query)["state"][0]
        token = {"access_token": "AT-new", "refresh_token": "RT-new", "expires_in": 1800}
        with mock.patch.object(requests, "post", return_value=FakeResponse(200, token)) as post, \
                mock.patch.object(requests, "get", side_effect=fake_get):
            r = self.client.get(f"/callback?code=abc&state={state}")
        return r, post

    def test_callback_success_saves_token_and_tenant(self):
        r, post = self._connect()
        self.assertEqual(r.status_code, 200)
        self.assertIn("Successfully Connected to Xero", r.get_data(as_text=True))
        self.assertEqual(post.call_args.kwargs["data"]["redirect_uri"], "http://localhost:5000/callback")
        saved = app_module.load_token()
        self.assertEqual((saved["access_token"], saved["tenant_id"], saved["tenant_name"]), ("AT-new", "tenant-1", "Demo Co"))

    def test_callback_error_and_missing_code(self):
        self.assertEqual(self.client.get("/callback?error=access_denied").status_code, 400)
        self.assertEqual(self.client.get("/callback").status_code, 400)

    def test_extract_validation(self):
        self.write_token()
        self.assertEqual(self.client.get("/extract/download/all").status_code, 400)
        self.assertEqual(self.client.get("/extract/download/nope?start=2025-01-01&end=2025-01-31").status_code, 400)

    def test_extract_all_produces_four_sheets_with_expected_rows(self):
        self.write_token()
        with mock.patch.object(requests, "get", side_effect=fake_get):
            r = self.client.get("/extract/download/all?start=2025-01-01&end=2025-01-31")
        self.assertEqual(r.status_code, 200)
        s = sheets(r)
        self.assertEqual(list(s), ["Invoice Allocations", "Bill Allocations", "Receive Overpayment Allocations", "Spend Overpayment Allocations"])
        inv, bill = s["Invoice Allocations"][0], s["Bill Allocations"][0]
        self.assertEqual((inv["CreditNoteNumber"], inv["CreditNoteId"], inv["AppliedAmount"], inv["InvoiceNumber"]), ("CN-001", "cn-1", 100.0, "INV-1"))
        self.assertEqual(inv["CreditNoteDate"], "2025-01-01")
        self.assertEqual((bill["CreditNoteNumber"], bill["AppliedAmount"], bill["InvoiceNumber"]), ("SUP-REF", 75.5, "BILL-9"))
        self.assertEqual(s["Receive Overpayment Allocations"][0]["CreditNoteID"], "op-1")
        self.assertEqual(s["Spend Overpayment Allocations"][0]["InvoiceNumber"], "BILL-3")

    def test_extract_single_type_and_empty_message(self):
        self.write_token()
        with mock.patch.object(requests, "get", side_effect=fake_get):
            r = self.client.get("/extract/download/invoice?start=2025-01-01&end=2025-01-31")
        self.assertIn("Invoice Allocations", sheets(r))
        empty = lambda url, headers=None, **kw: FakeResponse(200, {"CreditNotes": [], "Overpayments": []})  # noqa: E731
        with mock.patch.object(requests, "get", side_effect=empty):
            r = self.client.get("/extract/download/bill?start=2025-01-01&end=2025-01-31")
        self.assertIn("No Bill Allocations found", json.dumps(sheets(r)))

    def test_extract_sends_tenant_and_bearer_headers(self):
        self.write_token()
        with mock.patch.object(requests, "get", side_effect=fake_get) as get:
            self.client.get("/extract/download/all?start=2025-01-01&end=2025-01-31")
        h = get.call_args_list[0].kwargs["headers"]
        self.assertEqual((h["Authorization"], h["xero-tenant-id"]), ("Bearer AT-secret-1", "tenant-1"))

    def test_expired_token_is_refreshed_and_persisted(self):
        self.write_token(created_at=time.time() - 5000)
        new = {"access_token": "AT-2", "refresh_token": "RT-2", "expires_in": 1800}
        with mock.patch.object(requests, "post", return_value=FakeResponse(200, new)) as post, \
                mock.patch.object(requests, "get", side_effect=fake_get):
            r = self.client.get("/extract/download/all?start=2025-01-01&end=2025-01-31")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["data"]["grant_type"], "refresh_token")
        saved = app_module.load_token()
        self.assertEqual((saved["access_token"], saved["refresh_token"], saved["tenant_id"]), ("AT-2", "RT-2", "tenant-1"))

    def test_extract_without_token_returns_error_json(self):
        r = self.client.get("/extract/download/all?start=2025-01-01&end=2025-01-31")
        self.assertEqual(r.status_code, 500)
        self.assertIn("error", r.get_json())

    def test_tenants_and_switch_tenant(self):
        self.write_token()
        with mock.patch.object(requests, "get", side_effect=fake_get):
            t = self.client.get("/tenants").get_json()
            self.assertEqual([x["tenantName"] for x in t["tenants"]], ["Demo Co", "Second Co"])
            r = self.client.post("/switch_tenant", json={"tenant_id": "tenant-2"})
        self.assertEqual(r.get_json()["new_tenant"], "Second Co")
        self.assertEqual(app_module.load_token()["tenant_id"], "tenant-2")
        with mock.patch.object(requests, "get", side_effect=fake_get):
            self.assertEqual(self.client.post("/switch_tenant", json={"tenant_id": "zzz"}).status_code, 404)
            self.assertEqual(self.client.post("/switch_tenant", json={}).status_code, 400)

    def test_reset_removes_token_file(self):
        self.write_token()
        self.assertTrue(os.path.exists(app_module.TOKEN_FILE))
        self.assertEqual(self.client.get("/reset").status_code, 200)
        self.assertFalse(os.path.exists(app_module.TOKEN_FILE))


if __name__ == "__main__":
    unittest.main()
