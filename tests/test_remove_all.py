"""'Remove all' support: work-list endpoint, time-boxed automatic chunks, and the cross-worker cache epoch."""
import requests
from unittest import mock

from allocation_removal import AllocationService, parse_list_query
from tests.fakexero import IDS, alloc
from tests.support import FakeResponse
from tests.test_allocation_removal import RemovalTestCase

CN, OP = "credit_note", "overpayment"
CTX = {"tenant_id": "tenant-1", "tenant_name": "Demo Co"}


def extra_id(n):
    return f"bbbb{n:04x}-bbbb-4bbb-8bbb-bbbbbbbb{n:04x}"


class RemoveAllTests(RemovalTestCase):
    def add_allocations(self, count):
        """Give CN-001 `count` extra allocations (ids extra_id(1..count))."""
        for n in range(1, count + 1):
            self.xero.credit_notes[0]["Allocations"].append(alloc(extra_id(n), 10, f"INV-X{n}"))

    def targets(self, module=CN, qs=""):
        return self.client.get(f"/allocations/api/{module}/targets?refresh=1&{qs}")

    def bulk(self, items, **extra):
        return self.post(CN, "remove-bulk", {"confirm": True, "items": items, **extra})

    def item(self, alloc_id, parent="cn1"):
        return {"parentId": IDS[parent], "allocationId": alloc_id}

    # ---- work list
    def test_targets_returns_every_removable_row_on_all_pages_and_is_read_only(self):
        self.add_allocations(30)
        body = self.targets(qs="pageSize=10&page=2").get_json()   # paging params must not limit the work list
        self.assertEqual(body["removable"], 32)                    # a1, a4 + 30 extras (CN-003 is VOIDED)
        self.assertEqual(body["total"], 33)                        # the voided row is counted but never a target
        self.assertEqual(len(body["items"]), 32)
        self.assertNotIn(IDS["a5"], {i["allocationId"] for i in body["items"]})
        first = next(i for i in body["items"] if i["allocationId"] == IDS["a1"])
        self.assertEqual((first["parentId"], first["expectedAmount"], first["label"], first["related"]),
                         (IDS["cn1"], 100, "CN-001", "INV-1"))
        self.assertEqual(self.xero.deletes(), [])

    def test_targets_honours_search_and_module(self):
        self.assertEqual(self.targets(qs="search=INV-4").get_json()["removable"], 1)
        self.assertEqual(self.targets(qs="search=nomatch").get_json()["removable"], 0)
        self.assertEqual(self.targets(module="overpayment").get_json()["items"][0]["parentId"], IDS["op1"])
        self.assertEqual(self.client.get("/allocations/api/nope/targets").status_code, 404)

    # ---- chunk requests
    def test_auto_chunks_allow_up_to_100_while_the_manual_limit_stays_50(self):
        self.add_allocations(60)
        items = [self.item(extra_id(n)) for n in range(1, 61)]
        manual = self.bulk(items)
        self.assertEqual((manual.status_code, manual.get_json()["errorCode"]), (400, "TOO_MANY_ITEMS"))
        self.assertEqual(self.xero.allocation_deletes(), [])
        auto = self.bulk(items, auto=True).get_json()
        self.assertEqual(auto["summary"]["succeeded"], 60)
        too_many = self.bulk([self.item(IDS["a1"])] * 101, auto=True)
        self.assertEqual((too_many.status_code, too_many.get_json()["errorCode"]), (400, "TOO_MANY_ITEMS"))

    def test_auto_chunk_still_needs_csrf_and_explicit_confirmation(self):
        item = self.item(IDS["a1"])
        r = self.client.post("/allocations/api/credit_note/remove-bulk", json={"confirm": True, "auto": True, "items": [item]})
        self.assertEqual(r.get_json()["errorCode"], "CSRF_REJECTED")
        r = self.post(CN, "remove-bulk", {"auto": True, "items": [item]})
        self.assertEqual(r.get_json()["errorCode"], "CONFIRMATION_REQUIRED")
        self.assertEqual(self.xero.allocation_deletes(), [])

    def test_time_budget_defers_the_rest_without_touching_xero_and_a_resend_finishes_them(self):
        self.add_allocations(3)
        clock = {"t": 1000.0}
        real_delete = self.service._delete_one

        def slow_delete(*args, **kwargs):
            real_delete(*args, **kwargs)
            clock["t"] += 15

        items = [self.item(extra_id(n)) for n in (1, 2, 3)]
        with mock.patch.object(self.service, "clock", new=lambda: clock["t"]), \
                mock.patch.object(self.service, "_delete_one", side_effect=slow_delete):
            first = self.service.remove_bulk(CTX, CN, items, budget=20)
            self.assertEqual([r["status"] for r in first["results"]], ["REMOVED_VERIFIED", "REMOVED_VERIFIED", "SKIPPED"])
            self.assertEqual(first["results"][2]["errorCode"], "TIME_BUDGET")
            self.assertEqual(len(self.xero.allocation_deletes()), 2, "deferred item must not reach Xero")
            again = self.service.remove_bulk(CTX, CN, [items[2]], budget=20)
        self.assertEqual(again["summary"]["succeeded"], 1)
        self.assertEqual(len(self.xero.allocation_deletes()), 3)

    def test_budget_always_lets_the_first_item_through_so_a_run_cannot_stall(self):
        self.add_allocations(1)
        with mock.patch.object(self.service, "clock", new=lambda: 5.0):
            res = self.service.remove_bulk(CTX, CN, [self.item(extra_id(1))], budget=0)
        self.assertEqual(res["summary"]["succeeded"], 1)

    def test_full_remove_all_flow_in_chunks_leaves_nothing_behind(self):
        self.add_allocations(45)
        work = self.targets().get_json()["items"]
        self.assertEqual(len(work), 47)
        removed = 0
        for i in range(0, len(work), 20):
            chunk = [{k: w[k] for k in ("parentId", "allocationId", "expectedAmount")} for w in work[i:i + 20]]
            removed += self.bulk(chunk, auto=True).get_json()["summary"]["succeeded"]
        self.assertEqual(removed, 47)
        after = self.list(CN).get_json()
        self.assertEqual([r["allocationId"] for r in after["rows"]], [IDS["a5"]])   # only the voided document's row is left

    def test_a_fatal_error_stops_the_chunk_and_reports_the_rest_as_skipped(self):
        self.add_allocations(3)
        self.xero.fail(lambda c: c["method"] == "DELETE", response=FakeResponse(403, {}))
        res = self.bulk([self.item(extra_id(n)) for n in (1, 2, 3)], auto=True).get_json()["results"]
        self.assertEqual([r["errorCode"] for r in res], ["INSUFFICIENT_SCOPE", "INSUFFICIENT_SCOPE", "INSUFFICIENT_SCOPE"])
        self.assertEqual([r["status"] for r in res], ["FAILED", "SKIPPED", "SKIPPED"])
        self.assertEqual(len(self.xero.allocation_deletes()), 1)

    # ---- the "count jumps back" bug: a removal on one worker must invalidate the other worker's cache
    def test_removal_on_one_worker_invalidates_the_cache_of_another_worker(self):
        worker_b = AllocationService(self.service.xero)   # a second gunicorn worker: own memory, same working directory
        f = parse_list_query({})
        before = worker_b.list_rows(CTX, CN, f)
        self.assertIn(IDS["a1"], {r["allocationId"] for r in before["rows"]})
        self.assertTrue(self.remove(CN, "cn1", "a1").get_json()["ok"])          # handled by worker A
        self.xero.calls.clear()
        after = worker_b.list_rows(CTX, CN, f)                                  # worker B, normal (non-refresh) load
        self.assertNotIn(IDS["a1"], {r["allocationId"] for r in after["rows"]})
        self.assertTrue([c for c in self.xero.calls if c["path"] == "/CreditNotes"], "B must refetch, not serve its stale cache")

    def test_cache_is_still_used_when_nothing_was_removed(self):
        worker_b = AllocationService(self.service.xero)
        f = parse_list_query({})
        worker_b.list_rows(CTX, CN, f)
        self.xero.calls.clear()
        worker_b.list_rows(CTX, CN, f)
        self.assertEqual(self.xero.calls, [])

    # ---- page
    def test_page_has_remove_all_controls_and_no_secrets(self):
        page = self.client.get("/allocations/").get_data(as_text=True)
        for needle in ('id="btn_remove_all"', 'id="modal_all"', 'id="progress"', "/page?", "auto: true"):
            self.assertIn(needle, page)
        self.assertNotIn("AT-secret-1", page)
        self.assertNotIn("RT-secret-1", page)

    def test_page_does_not_fetch_on_open_and_needs_an_explicit_fetch_with_dates(self):
        page = self.client.get("/allocations/").get_data(as_text=True)
        self.assertIn("Fetch from Xero", page)
        self.assertIn("render(); loadStats();", page)
        self.assertNotIn("load(false); loadStats();", page)
        self.assertEqual([c for c in self.xero.calls if c["path"] in ("/CreditNotes", "/Overpayments")], [],
                         "opening the page must not call Xero's list endpoints")


class PageFetchTests(RemovalTestCase):
    """The list is fetched one Xero page per HTTP request, so a big organisation cannot hit a server timeout."""

    def page(self, module=CN, qs=""):
        return self.client.get(f"/allocations/api/{module}/page?{qs}")

    def is_list(self, call):
        return call["method"] == "GET" and call["path"] in ("/CreditNotes", "/Overpayments")

    def test_returns_one_page_of_rows_with_hasmore_and_calls_xero_once(self):
        body = self.page().get_json()
        self.assertEqual((body["page"], body["hasMore"], body["documents"]), (1, False, 3))
        self.assertEqual({r["parentNumber"] for r in body["rows"]}, {"CN-001", "CN-003"})   # ACCPAYCREDIT is another module
        self.assertEqual(len([c for c in self.xero.calls if self.is_list(c)]), 1)
        self.assertEqual(self.xero.deletes(), [])

    def test_hasmore_is_true_when_a_full_page_comes_back(self):
        template = self.xero.overpayments[0]
        for n in range(99):
            self.xero.overpayments.append({**template, "OverpaymentID": f"cccc{n:04x}-cccc-4ccc-8ccc-cccccccc{n:04x}", "Allocations": []})
        self.assertTrue(self.page(OP, "page=1").get_json()["hasMore"])
        self.assertFalse(self.page(OP, "page=2").get_json()["hasMore"])

    def test_page_passes_filters_to_xero_and_is_never_cached(self):
        self.page(CN, "status=AUTHORISED&from=2025-01-02&to=2025-02-03&page=2")
        self.assertEqual(self.xero.calls[-1]["params"]["page"], 2)
        self.assertIn('Status=="AUTHORISED"', self.xero.calls[-1]["params"]["where"])
        self.xero.calls.clear()
        self.page(); self.page()
        self.assertEqual(len([c for c in self.xero.calls if self.is_list(c)]), 2)

    def test_page_number_is_range_checked(self):
        r = self.page(CN, "page=100000")
        self.assertEqual((r.status_code, r.get_json()["errorCode"]), (400, "PAGE_OUT_OF_RANGE"))
        self.assertEqual(self.client.get("/allocations/api/nope/page").status_code, 404)

    def test_rate_limit_is_answered_immediately_with_retry_after_and_never_slept_inside_the_request(self):
        self.xero.fail(self.is_list, response=FakeResponse(429, {}, headers={"Retry-After": "3"}))
        r = self.page()
        self.assertEqual((r.status_code, r.get_json()["errorCode"], r.get_json()["retryAfter"]), (429, "RATE_LIMITED", 3.0))
        self.sleep.assert_not_called()
        self.assertEqual(len([c for c in self.xero.calls if self.is_list(c)]), 1)

    def test_slow_xero_gives_a_fast_json_error_the_browser_can_retry(self):
        self.xero.fail(self.is_list, raises=requests.exceptions.Timeout())
        r = self.page()
        self.assertEqual((r.status_code, r.get_json()["errorCode"]), (504, "NETWORK_TIMEOUT"))
        self.assertEqual(self.page().status_code, 200)   # the retry works

    def test_long_rate_limit_on_a_removal_is_handed_back_not_slept_and_nothing_is_deleted(self):
        self.xero.fail(lambda c: c["method"] == "DELETE", response=FakeResponse(429, {}, headers={"Retry-After": "60"}))
        res = self.post(CN, "remove-bulk", {"confirm": True, "auto": True, "items": [
            {"parentId": IDS["cn1"], "allocationId": IDS["a1"]}, {"parentId": IDS["cn1"], "allocationId": IDS["a4"]}]}).get_json()["results"]
        self.assertEqual([r["errorCode"] for r in res], ["RATE_LIMITED", "RATE_LIMITED"])
        self.assertEqual([r["status"] for r in res], ["FAILED", "SKIPPED"])
        self.assertEqual((res[0]["retryAfter"], res[1]["retryAfter"]), (60.0, 60.0))
        self.sleep.assert_not_called()
        self.assertEqual(len(self.xero.allocation_deletes()), 1)   # only the attempt that Xero refused; nothing was removed
        again = self.post(CN, "remove-bulk", {"confirm": True, "auto": True, "items": [
            {"parentId": IDS["cn1"], "allocationId": IDS["a1"]}]}).get_json()
        self.assertEqual(again["summary"]["succeeded"], 1)   # sending the same item again afterwards works


def parent_reads(xero):
    return [c for c in xero.calls if c["method"] == "GET" and c["path"].count("/") == 2]


class GroupedRemovalTests(RemovalTestCase):
    """Allocations of one document are removed with 1 pre-flight + N DELETEs + 1 verification (N + 2 calls)."""

    def add_allocations(self, count):
        for n in range(1, count + 1):
            self.xero.credit_notes[0]["Allocations"].append(alloc(extra_id(n), 10, f"INV-X{n}"))

    def item(self, alloc_id, parent="cn1", **extra):
        return {"parentId": IDS[parent], "allocationId": alloc_id, **extra}

    def test_five_allocations_of_one_document_cost_seven_calls_not_fifteen(self):
        self.add_allocations(3)                                           # cn1 now holds a1, a4 + 3 extras
        items = [self.item(IDS["a1"]), self.item(IDS["a4"])] + [self.item(extra_id(n)) for n in (1, 2, 3)]
        res = self.service.remove_bulk(CTX, CN, items)
        self.assertEqual(res["summary"]["succeeded"], 5)
        self.assertEqual(len(parent_reads(self.xero)), 2, "one pre-flight and one verification for the whole document")
        self.assertEqual(len(self.xero.allocation_deletes()), 5, "every allocation is still its own DELETE")
        self.assertEqual(self.xero.credit_notes[0]["Allocations"], [])
        for r in res["results"]:
            self.assertEqual((r["ok"], r["status"]), (True, "REMOVED_VERIFIED"))
            self.assertEqual(r["verification"]["allocationPresentAfter"], False)
            self.assertEqual(r["parentNumber"], "CN-001")

    def test_a_single_allocation_still_costs_three_calls(self):
        res = self.service.remove_bulk(CTX, CN, [self.item(IDS["a1"])])
        self.assertEqual(res["summary"]["succeeded"], 1)
        self.assertEqual(len(parent_reads(self.xero)), 2)
        self.assertEqual(len(self.xero.allocation_deletes()), 1)

    def test_results_keep_the_request_order_when_documents_are_interleaved(self):
        items = [self.item(IDS["a1"]), self.item(IDS["a2"], "cn2"), self.item(IDS["a4"])]
        res = self.service.remove_bulk(CTX, CN, items)["results"]
        self.assertEqual([r["allocationId"] for r in res], [IDS["a1"], IDS["a2"], IDS["a4"]])
        # cn2 is a bill credit, so it is refused in the Credit Note module - the two cn1 allocations are unaffected
        self.assertEqual([r["status"] for r in res], ["REMOVED_VERIFIED", "FAILED", "REMOVED_VERIFIED"])
        self.assertEqual(res[1]["errorCode"], "WRONG_MODULE")

    def test_a_stale_amount_only_fails_that_allocation(self):
        items = [self.item(IDS["a1"], expectedAmount=999), self.item(IDS["a4"], expectedAmount=50)]
        res = self.service.remove_bulk(CTX, CN, items)["results"]
        self.assertEqual((res[0]["status"], res[0]["errorCode"]), ("FAILED", "STALE_DATA"))
        self.assertTrue(res[1]["ok"])
        self.assertEqual([c["path"].split("/")[-1] for c in self.xero.allocation_deletes()], [IDS["a4"]])

    def test_missing_allocation_in_a_group_is_reported_and_the_rest_continue(self):
        items = [self.item(IDS["a1"]), self.item(extra_id(9)), self.item(IDS["a4"])]
        res = self.service.remove_bulk(CTX, CN, items)["results"]
        self.assertEqual([r["ok"] for r in res], [True, False, True])
        self.assertEqual(res[1]["errorCode"], "ALLOCATION_NOT_FOUND")

    def test_group_where_xero_ignores_the_delete_is_never_reported_as_removed(self):
        self.xero.ignore_delete = True
        res = self.service.remove_bulk(CTX, CN, [self.item(IDS["a1"]), self.item(IDS["a4"])])["results"]
        for r in res:
            self.assertEqual((r["ok"], r["status"], r["errorCode"]), (False, "UNVERIFIED", "VERIFICATION_FAILED"))

    def test_failed_verification_fetch_marks_the_group_unverified(self):
        reads = {"n": 0}

        def second_read(call):
            if call["method"] == "GET" and call["path"] == f"/CreditNotes/{IDS['cn1']}":
                reads["n"] += 1
                return reads["n"] == 2
            return False
        self.xero.fail(second_read, response=FakeResponse(500, {}))
        res = self.service.remove_bulk(CTX, CN, [self.item(IDS["a1"]), self.item(IDS["a4"])])["results"]
        self.assertEqual([r["status"] for r in res], ["UNVERIFIED", "UNVERIFIED"])
        self.assertEqual(len(self.xero.allocation_deletes()), 2)

    def test_rate_limit_in_the_middle_of_a_group_stops_it_and_verifies_what_was_done(self):
        self.add_allocations(1)
        self.xero.fail(lambda c: c["method"] == "DELETE" and c["path"].endswith(IDS["a4"]),
                       response=FakeResponse(429, {}, headers={"Retry-After": "100"}))
        items = [self.item(IDS["a1"]), self.item(IDS["a4"]), self.item(extra_id(1))]
        res = self.service.remove_bulk(CTX, CN, items)["results"]
        self.assertEqual(res[0]["status"], "REMOVED_VERIFIED")
        self.assertEqual((res[1]["status"], res[1]["errorCode"], res[1]["retryAfter"]), ("FAILED", "RATE_LIMITED", 100.0))
        self.assertEqual((res[2]["status"], res[2]["errorCode"], res[2]["retryAfter"]), ("SKIPPED", "RATE_LIMITED", 100.0))
        self.assertEqual(len(self.xero.allocation_deletes()), 2, "nothing is sent after the limit was hit")
        self.assertEqual(len(parent_reads(self.xero)), 2, "the allocation that was removed is still verified")

    def test_time_budget_inside_a_group_defers_the_rest_and_still_verifies_once(self):
        self.add_allocations(2)
        clock = {"t": 0.0}
        real = self.service._delete_one

        def slow(*a, **k):
            real(*a, **k)
            clock["t"] += 15
        items = [self.item(IDS["a1"]), self.item(IDS["a4"]), self.item(extra_id(1)), self.item(extra_id(2))]
        with mock.patch.object(self.service, "clock", new=lambda: clock["t"]), mock.patch.object(self.service, "_delete_one", side_effect=slow):
            res = self.service.remove_bulk(CTX, CN, items, budget=20)["results"]
        self.assertEqual([r["status"] for r in res], ["REMOVED_VERIFIED", "REMOVED_VERIFIED", "SKIPPED", "SKIPPED"])
        self.assertEqual({r["errorCode"] for r in res[2:]}, {"TIME_BUDGET"})
        self.assertEqual(len(self.xero.allocation_deletes()), 2)
        self.assertEqual(len(parent_reads(self.xero)), 2)

    def test_fatal_error_during_the_pre_flight_reports_once_and_skips_the_rest(self):
        self.xero.fail(lambda c: c["method"] == "GET" and c["path"] == f"/CreditNotes/{IDS['cn1']}", response=FakeResponse(403, {}))
        res = self.service.remove_bulk(CTX, CN, [self.item(IDS["a1"]), self.item(IDS["a4"])])["results"]
        self.assertEqual((res[0]["status"], res[0]["errorCode"]), ("FAILED", "INSUFFICIENT_SCOPE"))
        self.assertEqual((res[1]["status"], res[1]["errorCode"]), ("SKIPPED", "INSUFFICIENT_SCOPE"))
        self.assertEqual(self.xero.allocation_deletes(), [])

    def test_a_voided_document_fails_every_allocation_without_any_delete(self):
        res = self.service.remove_bulk(CTX, CN, [self.item(IDS["a5"], "cn3")])["results"]
        self.assertEqual(res[0]["errorCode"], "UNSUPPORTED_STATUS")
        self.assertEqual(self.xero.allocation_deletes(), [])

    def test_audit_log_gets_one_entry_per_allocation(self):
        self.service.remove_bulk(CTX, CN, [self.item(IDS["a1"]), self.item(IDS["a4"])])
        with open(self.service._audit_path(), encoding="utf-8") as fh:
            lines = [l for l in fh if l.strip()]
        self.assertEqual(len(lines), 2)
        self.assertNotIn("AT-secret", "".join(lines))


class CallBudgetTests(RemovalTestCase):
    def reading(self, day, age=0):
        client = self.service.xero
        client._limits["tenant-1"] = {"day": day, "minute": 50, "at": client.clock() - age}

    def test_remaining_calls_are_read_from_xeros_headers_and_returned_to_the_browser(self):
        self.xero.fail(lambda c: c["path"] == "/CreditNotes",
                       response=FakeResponse(200, {"CreditNotes": []}, headers={"X-DayLimit-Remaining": "4321", "X-MinLimit-Remaining": "55"}))
        body = self.client.get("/allocations/api/credit_note/page?page=1").get_json()
        self.assertEqual((body["limits"]["dayRemaining"], body["limits"]["minuteRemaining"]), (4321, 55))
        self.assertEqual(body["limits"]["reserve"], 50)

    def test_missing_headers_mean_unknown_not_zero(self):
        body = self.client.get("/allocations/api/credit_note/page?page=1").get_json()
        self.assertIsNone(body["limits"])

    def test_a_daily_429_is_remembered_as_zero_calls_left(self):
        self.xero.fail(lambda c: c["path"] == "/CreditNotes",
                       response=FakeResponse(429, {}, headers={"X-Rate-Limit-Problem": "day", "Retry-After": "3600"}))
        r = self.client.get("/allocations/api/credit_note/page?page=1")
        self.assertEqual(r.get_json()["errorCode"], "RATE_LIMIT_DAILY")
        self.assertEqual(self.service.xero.limits_for("tenant-1")["day"], 0)

    def test_run_stops_cleanly_before_the_daily_limit_without_calling_xero(self):
        self.reading(day=10)
        single = self.service.remove(CTX, CN, {"parentId": IDS["cn1"], "allocationId": IDS["a1"]})
        self.assertEqual((single["status"], single["errorCode"]), ("FAILED", "DAILY_BUDGET_LOW"))
        bulk = self.service.remove_bulk(CTX, CN, [{"parentId": IDS["cn1"], "allocationId": IDS["a1"]}, {"parentId": IDS["cn1"], "allocationId": IDS["a4"]},
                                                  {"parentId": IDS["cn2"], "allocationId": IDS["a2"]}])
        self.assertEqual([r["errorCode"] for r in bulk["results"]], ["DAILY_BUDGET_LOW"] * 3)
        self.assertEqual([r["status"] for r in bulk["results"]], ["FAILED", "SKIPPED", "SKIPPED"])
        self.assertEqual(self.xero.calls, [], "nothing at all was sent to Xero")

    def test_enough_budget_lets_the_run_proceed(self):
        self.reading(day=5000)
        self.assertTrue(self.service.remove(CTX, CN, {"parentId": IDS["cn1"], "allocationId": IDS["a1"]})["ok"])

    def test_a_stale_low_reading_never_blocks_the_tool_for_good(self):
        self.reading(day=0, age=3600)
        self.assertTrue(self.service.remove(CTX, CN, {"parentId": IDS["cn1"], "allocationId": IDS["a1"]})["ok"])

    def test_the_reserve_can_be_configured(self):
        self.reading(day=30)
        self.service.daily_reserve = 20
        self.assertTrue(self.service.remove(CTX, CN, {"parentId": IDS["cn1"], "allocationId": IDS["a1"]})["ok"])

    def test_group_stops_in_the_middle_when_the_budget_runs_low_and_still_verifies(self):
        for n in (1, 2):
            self.xero.credit_notes[0]["Allocations"].append(alloc(extra_id(n), 10, f"INV-X{n}"))
        real = self.service._delete_one

        def delete_then_drop_budget(*a, **k):
            real(*a, **k)
            self.reading(day=5)          # Xero now reports almost nothing left
        items = [{"parentId": IDS["cn1"], "allocationId": x} for x in (IDS["a1"], IDS["a4"], extra_id(1))]
        with mock.patch.object(self.service, "_delete_one", side_effect=delete_then_drop_budget):
            res = self.service.remove_bulk(CTX, CN, items)["results"]
        self.assertEqual([r["status"] for r in res], ["REMOVED_VERIFIED", "FAILED", "SKIPPED"])
        self.assertEqual(res[1]["errorCode"], "DAILY_BUDGET_LOW")
        self.assertEqual(len(self.xero.allocation_deletes()), 1)

    def test_status_and_bulk_responses_carry_the_limits(self):
        self.reading(day=4000)
        self.assertEqual(self.client.get("/allocations/api/status").get_json()["limits"]["dayRemaining"], 4000)
        body = self.post(CN, "remove-bulk", {"confirm": True, "items": [{"parentId": IDS["cn1"], "allocationId": IDS["a1"]}]}).get_json()
        self.assertIn("limits", body)
