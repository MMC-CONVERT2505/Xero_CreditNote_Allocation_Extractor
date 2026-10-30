import json
import os
import unittest
from unittest import mock

import requests

import xero_api
from tests.fakexero import FakeXero, IDS
from tests.support import AppTestCase, FakeResponse, app_module

CN, BC, OP = "credit_note", "bill_credit", "overpayment"


class RemovalTestCase(AppTestCase):
    def setUp(self):
        super().setUp()
        os.environ["ENABLE_ALLOCATION_REMOVAL"] = "true"
        self.write_token()
        self.xero = FakeXero()
        self.service = app_module.app.extensions["allocation_removal"]
        self.service._cache.clear()
        self.service._inflight.clear()
        stack = self.xero.patchers()
        stack.__enter__()
        self.addCleanup(stack.close)
        sleeps = mock.patch.object(xero_api, "_sleep")
        self.sleep = sleeps.start()
        self.addCleanup(sleeps.stop)

    # helpers
    def csrf(self):
        self.client.get("/allocations/")
        with self.client.session_transaction() as s:
            return s["csrf_token"]

    def post(self, module, action, body, csrf=True):
        headers = {"X-CSRF-Token": self.csrf()} if csrf is True else ({"X-CSRF-Token": csrf} if csrf else {})
        return self.client.post(f"/allocations/api/{module}/{action}", json=body, headers=headers)

    def remove(self, module, parent, alloc, **extra):
        return self.post(module, "remove", {"parentId": IDS[parent], "allocationId": IDS[alloc], "confirm": True, **extra})

    def list(self, module, qs=""):
        return self.client.get(f"/allocations/api/{module}/list?refresh=1&{qs}")

    def parent(self, resource, key):
        return self.xero.request("GET", f"{xero_api.BASE_URL}/{resource}/{IDS[key]}", headers={"Authorization": "Bearer AT-secret-1"})


class FeatureFlagTests(AppTestCase):
    def test_disabled_by_default_everything_404_and_no_link_on_home(self):
        self.write_token()
        for method, url in (("get", "/allocations/"), ("get", "/allocations/api/status"),
                            ("get", "/allocations/api/credit_note/list"), ("post", "/allocations/api/credit_note/remove")):
            r = getattr(self.client, method)(url)
            self.assertEqual(r.status_code, 404, url)
            self.assertEqual(r.get_json()["errorCode"], "FEATURE_DISABLED")
        self.assertNotIn("Allocation Removal", self.client.get("/").get_data(as_text=True))

    def test_flag_without_strong_secret_key_is_refused(self):
        os.environ["ENABLE_ALLOCATION_REMOVAL"] = "true"
        for bad in ("", "fixed_secret_key_2024"):
            with mock.patch.dict(os.environ, {"SECRET_KEY": bad}):
                r = self.client.get("/allocations/")
                self.assertEqual(r.status_code, 503)
                self.assertEqual(r.get_json()["errorCode"], "SECRET_KEY_REQUIRED")
                self.assertNotIn("Allocation Removal", self.client.get("/").get_data(as_text=True))

    def test_enabled_shows_link_and_page_renders_with_csrf_meta(self):
        self.write_token()
        os.environ["ENABLE_ALLOCATION_REMOVAL"] = "true"
        self.assertIn("Open Allocation Removal", self.client.get("/").get_data(as_text=True))
        page = self.client.get("/allocations/").get_data(as_text=True)
        with self.client.session_transaction() as s:
            self.assertIn(s["csrf_token"], page)
        self.assertNotIn("AT-secret-1", page)
        self.assertNotIn("RT-secret-1", page)


class ListingTests(RemovalTestCase):
    def test_modules_split_by_type_and_use_the_right_resource(self):
        cn = self.list(CN).get_json()
        self.assertEqual({r["parentNumber"] for r in cn["rows"]}, {"CN-001", "CN-003"})
        bc = self.list(BC).get_json()
        self.assertEqual([r["parentNumber"] for r in bc["rows"]], ["SCN-001"])
        op = self.list(OP).get_json()
        self.assertEqual(op["rows"][0]["parentId"], IDS["op1"])
        gets = [c for c in self.xero.calls if c["method"] == "GET"]
        self.assertEqual({c["path"] for c in gets}, {"/CreditNotes", "/Overpayments"})

    def test_rows_expose_ids_amounts_and_details(self):
        row = next(r for r in self.list(CN).get_json()["rows"] if r["allocationId"] == IDS["a1"])
        self.assertEqual((row["parentId"], row["allocationAmount"], row["relatedDocumentNumber"], row["contactName"]),
                         (IDS["cn1"], 100, "INV-1", "Acme"))
        self.assertEqual(row["allocationDate"], "2025-01-01")
        self.assertTrue(row["removable"])
        op = self.list(OP).get_json()["rows"][0]
        self.assertEqual(op["allocationAmount"], 120)  # falls back to Amount

    def test_voided_documents_are_listed_but_not_removable(self):
        row = next(r for r in self.list(CN).get_json()["rows"] if r["parentNumber"] == "CN-003")
        self.assertFalse(row["removable"])
        self.assertIn("VOIDED", row["notRemovableReason"])

    def test_missing_allocation_id_is_not_removable(self):
        del self.xero.credit_notes[0]["Allocations"][0]["AllocationID"]
        rows = self.list(CN).get_json()["rows"]
        self.assertTrue(any(not r["removable"] and "AllocationID" in r["notRemovableReason"] for r in rows))

    def test_where_clause_is_built_only_from_whitelisted_values(self):
        self.list(BC, 'status=AUTHORISED&from=2025-01-02&to=2025-02-03')
        self.assertEqual(self.xero.calls[-1]["params"]["where"],
                         'Type=="ACCPAYCREDIT" AND Status=="AUTHORISED" AND Date>=DateTime(2025,01,02) AND Date<=DateTime(2025,02,03)')
        self.xero.calls.clear()
        self.list(CN, 'status=X%22%20OR%201%3D1&from=2025-01-01%22)&to=bad')
        where = self.xero.calls[-1]["params"]["where"]
        self.assertEqual(where, 'Type=="ACCRECCREDIT"')

    def test_search_pagination_cache_and_auth(self):
        self.assertEqual(self.list(CN, "search=acme").get_json()["total"], 2)
        self.assertEqual(self.list(CN, "search=nomatch").get_json()["total"], 0)
        self.assertEqual(self.list(CN, "pageSize=10&page=99").get_json()["page"], 1)
        self.xero.calls.clear()
        self.service._cache.clear()
        self.client.get("/allocations/api/credit_note/list")
        self.client.get("/allocations/api/credit_note/list")
        self.assertEqual(len([c for c in self.xero.calls if c["path"] == "/CreditNotes"]), 1, "second call served from cache")
        os.remove(app_module.TOKEN_FILE)
        r = self.client.get("/allocations/api/credit_note/list")
        self.assertEqual((r.status_code, r.get_json()["errorCode"]), (401, "NOT_CONNECTED"))
        self.assertEqual(self.client.get("/allocations/api/nope/list").status_code, 404)

    def test_requests_carry_bearer_and_tenant_headers(self):
        self.list(CN)
        c = self.xero.calls[0]
        self.assertEqual((c["token"], c["tenant"]), ("AT-secret-1", "tenant-1"))


class RemovalTests(RemovalTestCase):
    def assert_only_allocation_endpoint_deleted(self, module_path, parent, alloc):
        deletes = self.xero.deletes()
        self.assertEqual([d["path"] for d in deletes], [f"/{module_path}/{IDS[parent]}/Allocations/{IDS[alloc]}"])

    def test_credit_note_removal_deletes_only_the_selected_allocation(self):
        r = self.remove(CN, "cn1", "a1")
        body = r.get_json()
        self.assertEqual((r.status_code, body["status"], body["ok"]), (200, "REMOVED_VERIFIED", True))
        self.assertTrue(body["verification"]["documentStillExists"])
        self.assertFalse(body["verification"]["allocationPresentAfter"])
        self.assert_only_allocation_endpoint_deleted("CreditNotes", "cn1", "a1")
        cn1 = self.xero.credit_notes[0]
        self.assertEqual([a["AllocationID"] for a in cn1["Allocations"]], [IDS["a4"]], "sibling allocation untouched")
        self.assertEqual(len(self.xero.credit_notes), 3, "no document deleted")

    def test_bill_credit_removal(self):
        r = self.remove(BC, "cn2", "a2")
        self.assertEqual(r.get_json()["status"], "REMOVED_VERIFIED")
        self.assert_only_allocation_endpoint_deleted("CreditNotes", "cn2", "a2")
        self.assertEqual(self.xero.credit_notes[1]["Allocations"], [])
        self.assertEqual(self.xero.credit_notes[1]["Status"], "AUTHORISED")

    def test_overpayment_removal(self):
        r = self.remove(OP, "op1", "a3")
        self.assertEqual(r.get_json()["status"], "REMOVED_VERIFIED")
        self.assert_only_allocation_endpoint_deleted("Overpayments", "op1", "a3")
        self.assertEqual(len(self.xero.overpayments), 1)

    def test_no_request_other_than_get_and_the_allocation_delete_is_ever_sent(self):
        self.remove(CN, "cn1", "a1")
        for c in self.xero.calls:
            self.assertIn(c["method"], ("GET", "DELETE"))
            if c["method"] == "DELETE":
                self.assertRegex(c["path"], r"^/(CreditNotes|Overpayments)/[^/]+/Allocations/[^/]+$")

    def test_bill_credit_cannot_be_removed_through_credit_note_module_and_vice_versa(self):
        r = self.remove(CN, "cn2", "a2")
        self.assertEqual((r.status_code, r.get_json()["errorCode"]), (422, "WRONG_MODULE"))
        self.assertIn("Bill Credit", r.get_json()["message"])
        r = self.remove(BC, "cn1", "a1")
        self.assertEqual(r.get_json()["errorCode"], "WRONG_MODULE")
        self.assertEqual(self.xero.deletes(), [])

    def test_invalid_ids_unknown_document_and_unknown_allocation_send_no_delete(self):
        bad = self.post(CN, "remove", {"parentId": "not-a-guid", "allocationId": IDS["a1"], "confirm": True})
        self.assertEqual(bad.get_json()["errorCode"], "INVALID_PARENT_ID")
        bad = self.post(CN, "remove", {"parentId": IDS["cn1"], "allocationId": "x'; DROP", "confirm": True})
        self.assertEqual(bad.get_json()["errorCode"], "INVALID_ALLOCATION_ID")
        unknown = self.post(CN, "remove", {"parentId": "99999999-9999-4999-8999-999999999999", "allocationId": IDS["a1"], "confirm": True})
        self.assertEqual((unknown.status_code, unknown.get_json()["errorCode"]), (404, "INVALID_PARENT_ID"))
        other_doc = self.remove(CN, "cn1", "a2")  # allocation belongs to a different document
        self.assertEqual(other_doc.get_json()["errorCode"], "ALLOCATION_NOT_FOUND")
        self.assertEqual(self.xero.deletes(), [])

    def test_voided_document_is_blocked(self):
        r = self.remove(CN, "cn3", "a5")
        self.assertEqual((r.status_code, r.get_json()["errorCode"]), (422, "UNSUPPORTED_STATUS"))
        self.assertEqual(self.xero.deletes(), [])

    def test_repeating_a_removal_does_not_send_a_second_delete(self):
        self.assertTrue(self.remove(CN, "cn1", "a1").get_json()["ok"])
        again = self.remove(CN, "cn1", "a1")
        self.assertEqual((again.status_code, again.get_json()["errorCode"]), (404, "ALLOCATION_NOT_FOUND"))
        self.assertEqual(len(self.xero.allocation_deletes()), 1)

    def test_stale_expected_amount_is_rejected_before_delete(self):
        r = self.remove(CN, "cn1", "a1", expectedAmount=999)
        self.assertEqual((r.status_code, r.get_json()["errorCode"]), (409, "STALE_DATA"))
        self.assertEqual(self.xero.deletes(), [])
        ok = self.remove(CN, "cn1", "a1", expectedAmount=100)
        self.assertTrue(ok.get_json()["ok"])

    def test_duplicate_in_flight_request_is_rejected(self):
        self.service._inflight.add(f"tenant-1:{IDS['a1']}")
        r = self.remove(CN, "cn1", "a1")
        self.assertEqual((r.status_code, r.get_json()["errorCode"]), (409, "DUPLICATE_IN_PROGRESS"))
        self.assertEqual(self.xero.deletes(), [])

    def test_inflight_marker_is_released_after_success_and_failure(self):
        self.remove(CN, "cn1", "a1")
        self.remove(CN, "cn3", "a5")
        self.assertEqual(self.service._inflight, set())

    def test_accepted_but_still_present_is_reported_unverified_not_success(self):
        self.xero.ignore_delete = True
        r = self.remove(CN, "cn1", "a1")
        body = r.get_json()
        self.assertEqual((r.status_code, body["ok"], body["status"], body["errorCode"]), (502, False, "UNVERIFIED", "VERIFICATION_FAILED"))


class XeroFailureTests(RemovalTestCase):
    def is_delete(self, c):
        return c["method"] == "DELETE"

    def test_timeout_after_xero_processed_does_not_delete_twice(self):
        self.xero.fail(self.is_delete, raises=requests.exceptions.Timeout(), apply_delete=True)
        r = self.remove(CN, "cn1", "a1")
        self.assertEqual((r.status_code, r.get_json()["status"]), (200, "REMOVED_VERIFIED"))
        self.assertEqual(len(self.xero.allocation_deletes()), 1)

    def test_timeout_before_xero_processed_is_retried_exactly_once(self):
        self.xero.fail(self.is_delete, raises=requests.exceptions.ConnectionError())
        r = self.remove(CN, "cn1", "a1")
        self.assertEqual(r.get_json()["status"], "REMOVED_VERIFIED")
        self.assertEqual(len(self.xero.allocation_deletes()), 2)

    def test_persistent_timeout_gives_up_after_one_retry(self):
        for _ in range(3):
            self.xero.fail(self.is_delete, raises=requests.exceptions.Timeout())
        r = self.remove(CN, "cn1", "a1")
        self.assertEqual((r.get_json()["ok"], r.get_json()["errorCode"]), (False, "NETWORK_TIMEOUT"))
        self.assertEqual(len(self.xero.allocation_deletes()), 2)

    def test_5xx_is_ambiguous_and_checked_before_retry(self):
        self.xero.fail(self.is_delete, response=FakeResponse(503, {}), apply_delete=True)
        r = self.remove(CN, "cn1", "a1")
        self.assertEqual(r.get_json()["status"], "REMOVED_VERIFIED")
        self.assertEqual(len(self.xero.allocation_deletes()), 1)

    def test_state_unknown_after_timeout_is_unverified_and_not_retried(self):
        self.xero.fail(self.is_delete, raises=requests.exceptions.Timeout(), apply_delete=True)
        after_delete = lambda c: c["method"] == "GET" and c["path"].startswith("/CreditNotes/") and bool(self.xero.deletes())
        self.xero.fail(after_delete, raises=requests.exceptions.Timeout())
        r = self.remove(CN, "cn1", "a1")
        body = r.get_json()
        self.assertEqual((body["ok"], body["status"], body["errorCode"]), (False, "UNVERIFIED", "OUTCOME_UNKNOWN"))
        self.assertEqual(len(self.xero.allocation_deletes()), 1, "must not blindly retry when the state is unknown")

    def test_xero_validation_error_is_shown_and_not_retried(self):
        resp = FakeResponse(400, {"Elements": [{"ValidationErrors": [{"Message": "Cannot remove allocation: period is locked"}]}]})
        self.xero.fail(self.is_delete, response=resp)
        r = self.remove(CN, "cn1", "a1")
        self.assertEqual((r.status_code, r.get_json()["errorCode"]), (400, "XERO_VALIDATION"))
        self.assertIn("period is locked", r.get_json()["message"])
        self.assertEqual(len(self.xero.allocation_deletes()), 1)
        self.assertEqual(len(self.xero.credit_notes[0]["Allocations"]), 2)

    def test_403_reports_scope_problem_without_retry(self):
        self.xero.fail(self.is_delete, response=FakeResponse(403, {}))
        r = self.remove(OP, "op1", "a3")
        self.assertEqual((r.status_code, r.get_json()["errorCode"]), (403, "INSUFFICIENT_SCOPE"))
        self.assertEqual(len(self.xero.allocation_deletes()), 1)

    def test_429_waits_retry_after_then_succeeds(self):
        self.xero.fail(self.is_delete, response=FakeResponse(429, {}, headers={"Retry-After": "7"}))
        r = self.remove(CN, "cn1", "a1")
        self.assertEqual(r.get_json()["status"], "REMOVED_VERIFIED")
        self.sleep.assert_called_once_with(7.0)
        self.assertEqual(len(self.xero.allocation_deletes()), 2)

    def test_daily_rate_limit_is_not_retried(self):
        self.xero.fail(self.is_delete, response=FakeResponse(429, {}, headers={"Retry-After": "60", "X-Rate-Limit-Problem": "day"}))
        r = self.remove(CN, "cn1", "a1")
        self.assertEqual((r.status_code, r.get_json()["errorCode"]), (429, "RATE_LIMIT_DAILY"))
        self.sleep.assert_not_called()
        self.assertEqual(len(self.xero.allocation_deletes()), 1)

    def test_rate_limit_retries_are_capped(self):
        for _ in range(10):
            self.xero.fail(self.is_delete, response=FakeResponse(429, {}, headers={"Retry-After": "1"}))
        r = self.remove(CN, "cn1", "a1")
        self.assertEqual(r.get_json()["errorCode"], "RATE_LIMITED")
        self.assertEqual(len(self.xero.allocation_deletes()), 4)  # first try + 3 retries

    def test_rejected_access_token_is_refreshed_once_and_retried(self):
        self.xero.valid_tokens = {"somebody-else"}
        r = self.remove(CN, "cn1", "a1")
        self.assertEqual(self.xero.refresh_count, 1)
        self.assertEqual(r.get_json()["status"], "REMOVED_VERIFIED")
        self.assertEqual(app_module.load_token()["access_token"], "AT-2")

    def test_refresh_that_still_fails_does_not_loop(self):
        self.xero.valid_tokens = set()
        with mock.patch.object(requests, "post", return_value=FakeResponse(400, {"error": "invalid_grant"})):
            r = self.remove(CN, "cn1", "a1")
        self.assertFalse(r.get_json()["ok"])
        self.assertEqual(self.xero.deletes(), [])


class GuardTests(RemovalTestCase):
    def test_missing_or_wrong_csrf_is_403_and_nothing_is_sent_to_xero(self):
        body = {"parentId": IDS["cn1"], "allocationId": IDS["a1"], "confirm": True}
        for csrf in (False, "wrong-token"):
            r = self.post(CN, "remove", body, csrf=csrf)
            self.assertEqual((r.status_code, r.get_json()["errorCode"]), (403, "CSRF_REJECTED"))
        self.assertEqual(self.xero.calls, [])

    def test_csrf_from_another_session_is_rejected(self):
        token = self.csrf()
        other = app_module.app.test_client()
        r = other.post("/allocations/api/credit_note/remove", json={"confirm": True}, headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 403)

    def test_confirmation_is_mandatory_and_must_be_literal_true(self):
        for confirm in (None, False, "true", 1, "yes"):
            body = {"parentId": IDS["cn1"], "allocationId": IDS["a1"]}
            if confirm is not None:
                body["confirm"] = confirm
            r = self.post(CN, "remove", body)
            self.assertEqual((r.status_code, r.get_json()["errorCode"]), (400, "CONFIRMATION_REQUIRED"), confirm)
        self.assertEqual(self.xero.calls, [])

    def test_non_json_body_and_unknown_module_rejected(self):
        r = self.client.post("/allocations/api/credit_note/remove", data="x", headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(r.get_json()["errorCode"], "BAD_JSON")
        r = self.post("hack", "remove", {"confirm": True})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.xero.calls, [])

    def test_get_cannot_trigger_removal(self):
        self.assertEqual(self.client.get(f"/allocations/api/credit_note/remove").status_code, 405)

    def test_not_connected_is_401(self):
        token = self.csrf()
        os.remove(app_module.TOKEN_FILE)
        r = self.client.post("/allocations/api/credit_note/remove", json={"confirm": True}, headers={"X-CSRF-Token": token})
        self.assertEqual((r.status_code, r.get_json()["errorCode"]), (401, "NOT_CONNECTED"))

    def test_tenant_allowlist_blocks_removal_but_not_listing(self):
        os.environ["REMOVAL_ALLOWED_TENANT_IDS"] = "some-other-tenant"
        r = self.remove(CN, "cn1", "a1")
        self.assertEqual((r.status_code, r.get_json()["errorCode"]), (403, "TENANT_NOT_ALLOWED"))
        self.assertEqual(self.xero.deletes(), [])
        self.assertEqual(self.list(CN).status_code, 200)
        self.assertFalse(self.client.get("/allocations/api/status").get_json()["removalAllowed"])
        os.environ["REMOVAL_ALLOWED_TENANT_IDS"] = "TENANT-1, other"
        self.assertTrue(self.remove(CN, "cn1", "a1").get_json()["ok"])

    def test_audit_log_records_ids_and_outcomes_but_no_secrets(self):
        self.remove(CN, "cn1", "a1")
        self.remove(CN, "cn3", "a5")
        with open(os.environ.get("ALLOCATION_AUDIT_LOG") or "allocation_removal_audit.log") as fh:
            text = fh.read()
        entries = [json.loads(line) for line in text.splitlines()]
        self.assertEqual([e["ok"] for e in entries], [True, False])
        self.assertEqual(entries[0]["allocationId"], IDS["a1"])
        for secret in ("AT-secret", "RT-secret", "test-client-secret", "Bearer"):
            self.assertNotIn(secret, text)
        self.assertEqual(self.client.get("/allocations/api/status").get_json()["stats"], {"succeeded": 1, "failed": 1})

    def test_responses_never_contain_tokens(self):
        outputs = [self.list(CN).get_data(as_text=True), self.remove(CN, "cn1", "a1").get_data(as_text=True),
                   self.remove(CN, "cn1", "a1").get_data(as_text=True), self.client.get("/allocations/").get_data(as_text=True)]
        for text in outputs:
            for secret in ("AT-secret", "RT-secret", "test-client-secret"):
                self.assertNotIn(secret, text)

    def test_logs_do_not_contain_tokens(self):
        with self.assertLogs(level="DEBUG") as logs:
            import logging
            logging.getLogger("allocation_removal").debug("probe")
            self.xero.ignore_delete = True
            self.remove(CN, "cn1", "a1")
        joined = "\n".join(logs.output)
        for secret in ("AT-secret", "RT-secret", "test-client-secret"):
            self.assertNotIn(secret, joined)


class BulkTests(RemovalTestCase):
    def items(self, *pairs):
        return [{"parentId": IDS[p], "allocationId": IDS[a]} for p, a in pairs]

    def bulk(self, module, items, **kw):
        return self.post(module, "remove-bulk", {"items": items, "confirm": True, **kw})

    def test_all_selected_allocations_removed_independently(self):
        r = self.bulk(CN, self.items(("cn1", "a1"), ("cn1", "a4")))
        body = r.get_json()
        self.assertEqual(body["summary"], {"total": 2, "succeeded": 2, "failed": 0, "skipped": 0})
        self.assertEqual(self.xero.credit_notes[0]["Allocations"], [])
        self.assertEqual(len(self.xero.allocation_deletes()), 2)

    def test_partial_success_reports_each_item(self):
        r = self.bulk(CN, self.items(("cn1", "a1"), ("cn3", "a5"), ("cn1", "a4")))
        body = r.get_json()
        self.assertEqual([x["ok"] for x in body["results"]], [True, False, True])
        self.assertEqual(body["results"][1]["errorCode"], "UNSUPPORTED_STATUS")
        self.assertEqual(body["summary"]["succeeded"], 2)
        self.assertEqual(body["summary"]["failed"], 1)

    def test_duplicates_in_one_request_are_skipped(self):
        body = self.bulk(CN, self.items(("cn1", "a1"), ("cn1", "a1"))).get_json()
        self.assertEqual([x["status"] for x in body["results"]], ["REMOVED_VERIFIED", "SKIPPED"])
        self.assertEqual(body["results"][1]["errorCode"], "DUPLICATE_IN_REQUEST")
        self.assertEqual(len(self.xero.allocation_deletes()), 1)

    def test_fatal_error_aborts_remaining_items(self):
        self.xero.fail(lambda c: c["method"] == "DELETE",
                       response=FakeResponse(429, {}, headers={"X-Rate-Limit-Problem": "day"}))
        body = self.bulk(CN, self.items(("cn1", "a1"), ("cn1", "a4"))).get_json()
        self.assertEqual(body["results"][0]["errorCode"], "RATE_LIMIT_DAILY")
        self.assertEqual(body["results"][1]["status"], "SKIPPED")
        self.assertEqual(body["summary"], {"total": 2, "succeeded": 0, "failed": 1, "skipped": 1})
        self.assertEqual(len(self.xero.allocation_deletes()), 1)

    def test_item_limits_and_empty_selection(self):
        too_many = [{"parentId": IDS["cn1"], "allocationId": f"aaaaaaa1-aaaa-4aaa-8aaa-{i:012d}"} for i in range(51)]
        r = self.bulk(CN, too_many)
        self.assertEqual((r.status_code, r.get_json()["errorCode"]), (400, "TOO_MANY_ITEMS"))
        self.assertEqual(self.bulk(CN, []).get_json()["errorCode"], "NO_ITEMS")
        self.assertEqual(self.xero.deletes(), [])

    def test_bulk_requires_csrf_and_confirmation(self):
        r = self.post(CN, "remove-bulk", {"items": self.items(("cn1", "a1")), "confirm": True}, csrf=False)
        self.assertEqual(r.status_code, 403)
        r = self.post(CN, "remove-bulk", {"items": self.items(("cn1", "a1"))})
        self.assertEqual(r.get_json()["errorCode"], "CONFIRMATION_REQUIRED")
        self.assertEqual(self.xero.calls, [])

    def test_malformed_items_do_not_crash(self):
        body = self.bulk(CN, ["junk", None, 5, {"parentId": IDS["cn1"]}]).get_json()
        self.assertEqual(body["summary"]["succeeded"], 0)
        self.assertEqual(len(body["results"]), 4)
        self.assertEqual(self.xero.deletes(), [])

    def test_bulk_works_for_overpayments_and_bill_credits(self):
        self.assertTrue(self.bulk(OP, self.items(("op1", "a3"))).get_json()["results"][0]["ok"])
        self.assertTrue(self.bulk(BC, self.items(("cn2", "a2"))).get_json()["results"][0]["ok"])


if __name__ == "__main__":
    unittest.main()
