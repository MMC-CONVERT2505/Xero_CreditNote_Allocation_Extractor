"""In-memory fake of the Xero endpoints used by the Allocation Removal feature (no network, no real tokens)."""
import contextlib
import re
from unittest import mock

import requests

from tests.support import FakeResponse

BASE = "https://api.xero.com/api.xro/2.0"
IDS = {
    "cn1": "11111111-1111-4111-8111-111111111111",   # ACCRECCREDIT, AUTHORISED (2 allocations)
    "cn2": "22222222-2222-4222-8222-222222222222",   # ACCPAYCREDIT, AUTHORISED
    "cn3": "33333333-3333-4333-8333-333333333333",   # ACCRECCREDIT, VOIDED
    "op1": "44444444-4444-4444-8444-444444444444",   # RECEIVE-OVERPAYMENT
    "a1": "aaaaaaa1-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
    "a2": "aaaaaaa2-aaaa-4aaa-8aaa-aaaaaaaaaaa2",
    "a3": "aaaaaaa3-aaaa-4aaa-8aaa-aaaaaaaaaaa3",
    "a4": "aaaaaaa4-aaaa-4aaa-8aaa-aaaaaaaaaaa4",
    "a5": "aaaaaaa5-aaaa-4aaa-8aaa-aaaaaaaaaaa5",
}


def alloc(alloc_id, amount, invoice_no, key="AppliedAmount"):
    return {"AllocationID": alloc_id, key: amount, "Date": "/Date(1735689600000+0000)/",
            "Invoice": {"InvoiceID": f"inv-{invoice_no}", "InvoiceNumber": invoice_no}}


class FakeXero:
    def __init__(self):
        self.valid_tokens = {"AT-secret-1"}
        self.refresh_count = 0
        self.calls = []
        self.failures = []          # {"when": fn(call), "response"|"raises": ..., "apply_delete": bool}
        self.ignore_delete = False  # simulate "200 OK but nothing changed"
        self.credit_notes = [
            {"CreditNoteID": IDS["cn1"], "CreditNoteNumber": "CN-001", "Type": "ACCRECCREDIT", "Status": "AUTHORISED", "Total": 300,
             "Contact": {"Name": "Acme"}, "CurrencyCode": "USD",
             "Allocations": [alloc(IDS["a1"], 100, "INV-1"), alloc(IDS["a4"], 50, "INV-4")]},
            {"CreditNoteID": IDS["cn2"], "CreditNoteNumber": "SCN-001", "Type": "ACCPAYCREDIT", "Status": "AUTHORISED", "Total": 200,
             "Contact": {"Name": "Supplier Ltd"}, "Allocations": [alloc(IDS["a2"], 75, "BILL-9")]},
            {"CreditNoteID": IDS["cn3"], "CreditNoteNumber": "CN-003", "Type": "ACCRECCREDIT", "Status": "VOIDED", "Total": 10,
             "Contact": {"Name": "Gone Co"}, "Allocations": [alloc(IDS["a5"], 10, "INV-5")]},
        ]
        self.overpayments = [
            {"OverpaymentID": IDS["op1"], "Type": "RECEIVE-OVERPAYMENT", "Status": "AUTHORISED", "Total": 500,
             "Contact": {"Name": "Over Payer"}, "Allocations": [alloc(IDS["a3"], 120, "INV-3", key="Amount")]},
        ]

    # ---- test helpers
    def fail(self, when, response=None, raises=None, apply_delete=False):
        self.failures.append({"when": when, "response": response, "raises": raises, "apply_delete": apply_delete})

    def deletes(self):
        return [c for c in self.calls if c["method"] == "DELETE"]

    def allocation_deletes(self):
        return [c for c in self.deletes() if "/Allocations/" in c["path"]]

    def patchers(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(requests, "request", side_effect=self.request))
        stack.enter_context(mock.patch.object(requests, "post", side_effect=self.post))
        return stack

    # ---- fake network
    def post(self, url, data=None, **kw):  # token endpoint (refresh)
        self.calls.append({"method": "POST", "path": url, "params": None, "token": None, "tenant": None})
        self.refresh_count += 1
        new = f"AT-{self.refresh_count + 1}"
        self.valid_tokens.add(new)
        return FakeResponse(200, {"access_token": new, "refresh_token": f"RT-{self.refresh_count}", "expires_in": 1800})

    def _remove_allocation(self, path):
        m = re.match(r"^/(CreditNotes|Overpayments)/([^/]+)/Allocations/([^/]+)$", path)
        lst, field = (self.overpayments, "OverpaymentID") if m.group(1) == "Overpayments" else (self.credit_notes, "CreditNoteID")
        parent = next((p for p in lst if p[field] == m.group(2)), None)
        idx = next((i for i, a in enumerate(parent["Allocations"]) if a["AllocationID"] == m.group(3)), -1) if parent else -1
        if idx < 0:
            return FakeResponse(404, {"Title": "Not Found"})
        if not self.ignore_delete:
            parent["Allocations"].pop(idx)
        return FakeResponse(200, {"Allocations": [{"AllocationID": m.group(3), "Amount": 1, "IsDeleted": True}]})

    def request(self, method, url, params=None, headers=None, timeout=None, **kw):
        path = url[len(BASE):]
        call = {"method": method, "path": path, "params": params, "tenant": (headers or {}).get("xero-tenant-id"),
                "token": (headers or {}).get("Authorization", "").replace("Bearer ", "")}
        self.calls.append(call)
        for i, f in enumerate(self.failures):
            if f["when"](call):
                self.failures.pop(i)
                if f["apply_delete"]:
                    self._remove_allocation(path)
                if f["raises"]:
                    raise f["raises"]
                return f["response"]
        if call["token"] not in self.valid_tokens:
            return FakeResponse(401, {})
        if method == "DELETE":
            return self._remove_allocation(path)
        if path == "/CreditNotes":
            return FakeResponse(200, {"CreditNotes": self._copy(self.credit_notes) if params["page"] == 1 else []})
        if path == "/Overpayments":
            return FakeResponse(200, {"Overpayments": self._copy(self.overpayments) if params["page"] == 1 else []})
        m = re.match(r"^/(CreditNotes|Overpayments)/([^/]+)$", path)
        if m:
            lst, field = (self.overpayments, "OverpaymentID") if m.group(1) == "Overpayments" else (self.credit_notes, "CreditNoteID")
            found = next((p for p in lst if p[field] == m.group(2)), None)
            return FakeResponse(200, {m.group(1): self._copy([found])}) if found else FakeResponse(404, {"Title": "Not Found"})
        return FakeResponse(404, {})

    @staticmethod
    def _copy(items):
        import copy
        return copy.deepcopy(items)
