"""
Allocation Removal: remove ALLOCATION LINKS from Credit Notes, Bill Credits (ACCPAYCREDIT) and Overpayments.

It never deletes the credit note / bill / invoice / overpayment itself. The only write call is
    DELETE /CreditNotes/{CreditNoteID}/Allocations/{AllocationID}
    DELETE /Overpayments/{OverpaymentID}/Allocations/{AllocationID}
(supplier credit notes are the same /CreditNotes resource, Type = ACCPAYCREDIT).

Disabled by default. Enable with ENABLE_ALLOCATION_REMOVAL=true AND a real SECRET_KEY.
"""
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, render_template, request, session, url_for

from xero_api import XeroApiError, XeroClient

log = logging.getLogger("allocation_removal")

MODULES = {
    "credit_note": {"label": "Credit Note", "resource": "CreditNotes", "id_field": "CreditNoteID", "type": "ACCRECCREDIT"},
    "bill_credit": {"label": "Bill Credit", "resource": "CreditNotes", "id_field": "CreditNoteID", "type": "ACCPAYCREDIT"},
    "overpayment": {"label": "Overpayment", "resource": "Overpayments", "id_field": "OverpaymentID", "type": None},
}
BLOCKED_STATUSES = {"VOIDED", "DELETED"}
FILTER_STATUSES = ("AUTHORISED", "PAID", "VOIDED")
PAGE_SIZE = 100          # Xero page size used while listing
MAX_BULK = 50
MAX_AUTO = 100           # "Remove all" sends chunks; a chunk may hold up to this many items
AUTO_BUDGET_SECONDS = 20  # a chunk stops starting new items after this long (keeps each HTTP request short)
# After one of these, the remaining items of a bulk request are skipped.
FATAL_CODES = {"NOT_CONNECTED", "TOKEN_REFRESH_FAILED", "XERO_UNAUTHORIZED", "INSUFFICIENT_SCOPE", "RATE_LIMIT_DAILY", "TOKEN_STORE_ERROR",
               "DAILY_BUDGET_LOW"}
DAILY_RESERVE_DEFAULT = 50   # stop on purpose when Xero says this few calls are left for the day (env XERO_DAILY_RESERVE)
LIMIT_MAX_AGE = 900          # a "calls left" reading older than this (seconds) is not trusted for stopping a run
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
LEGACY_SECRET_KEY = "fixed_secret_key_2024"


# ------------------------------------------------------------------------------------------- feature flag
def flag_on():
    return os.getenv("ENABLE_ALLOCATION_REMOVAL", "").strip().lower() in {"1", "true", "yes", "on"}


def secret_configured():
    key = os.getenv("SECRET_KEY", "")
    return bool(key) and key != LEGACY_SECRET_KEY


def removal_enabled():
    return flag_on() and secret_configured()


def allowed_tenants():
    return {t.strip().lower() for t in os.getenv("REMOVAL_ALLOWED_TENANT_IDS", "").split(",") if t.strip()}


def tenant_allowed(tenant_id):
    allow = allowed_tenants()
    return not allow or str(tenant_id).lower() in allow


# ------------------------------------------------------------------------------------------- helpers
def is_uuid(value):
    return isinstance(value, str) and bool(UUID_RE.match(value))


def parse_xero_date(value):
    """'/Date(1573776000000+0000)/' or ISO -> 'YYYY-MM-DD' (or None)."""
    if not value:
        return None
    text = str(value)
    m = re.match(r"^/Date\((-?\d+)([+-]\d{4})?\)/$", text)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    m = re.match(r"^\d{4}-\d{2}-\d{2}", text)
    return m.group(0) if m else None


def allocation_amount(alloc):
    """List responses use AppliedAmount (as the existing extractor assumes); fall back to Amount."""
    value = alloc.get("AppliedAmount")
    return alloc.get("Amount") if value is None else value


def normalize_row(module_key, parent, alloc, index=0):
    mod = MODULES[module_key]
    parent_id = parent.get(mod["id_field"])
    inv = alloc.get("Invoice") or {}
    allocation_id = alloc.get("AllocationID")
    reason = None
    if not allocation_id:
        reason = "Xero did not return an AllocationID for this allocation."
    elif parent.get("Status") in BLOCKED_STATUSES:
        reason = f"Document status is {parent.get('Status')}."
    return {
        "rowKey": f"{parent_id}:{allocation_id or 'noid-%d' % index}",
        "module": module_key,
        "parentId": parent_id,
        "parentNumber": parent.get("CreditNoteNumber"),
        "reference": parent.get("Reference"),
        "contactName": (parent.get("Contact") or {}).get("Name"),
        "type": parent.get("Type"),
        "status": parent.get("Status"),
        "currency": parent.get("CurrencyCode"),
        "date": parse_xero_date(parent.get("DateString") or parent.get("Date")),
        "total": parent.get("Total"),
        "appliedAmount": parent.get("AppliedAmount"),
        "remainingCredit": parent.get("RemainingCredit"),
        "allocationId": allocation_id,
        "allocationAmount": allocation_amount(alloc),
        "allocationDate": parse_xero_date(alloc.get("DateString") or alloc.get("Date")),
        "relatedDocumentId": inv.get("InvoiceID"),
        "relatedDocumentNumber": inv.get("InvoiceNumber"),
        "removable": reason is None,
        "notRemovableReason": reason,
    }


def _valid_date(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def parse_list_query(args):
    """Everything that reaches a Xero `where` clause is whitelisted or re-formatted here."""
    status = args.get("status", "")
    try:
        page_size = int(args.get("pageSize", 25))
    except ValueError:
        page_size = 25
    try:
        page = max(1, int(args.get("page", 1)))
    except ValueError:
        page = 1
    return {
        "status": status if status in FILTER_STATUSES else "",
        "from": _valid_date(args.get("from", "")),
        "to": _valid_date(args.get("to", "")),
        "search": (args.get("search") or "").strip()[:100],
        "page": page,
        "pageSize": page_size if page_size in (10, 25, 50, 100) else 25,
        "refresh": args.get("refresh") in ("1", "true"),
    }


def build_where(mod, f):
    parts = []
    if mod["type"]:
        parts.append(f'Type=="{mod["type"]}"')
    if f["status"]:
        parts.append(f'Status=="{f["status"]}"')
    if f["from"]:
        parts.append(f'Date>=DateTime({f["from"].replace("-", ",")})')  # same DateTime(YYYY,MM,DD) form the extractor uses
    if f["to"]:
        parts.append(f'Date<=DateTime({f["to"].replace("-", ",")})')
    return " AND ".join(parts)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------------------------------- service
class AllocationService:
    def __init__(self, xero, max_pages=None, cache_ttl=60, clock=time.time):
        self.xero = xero
        self.max_pages = max_pages or int(os.getenv("MAX_XERO_PAGES", "25"))
        self.fetch_max_pages = int(os.getenv("XERO_FETCH_MAX_PAGES", "200"))   # page-by-page fetch: 200 x 100 documents
        try:
            self.daily_reserve = max(0, int(os.getenv("XERO_DAILY_RESERVE", str(DAILY_RESERVE_DEFAULT))))
        except ValueError:
            self.daily_reserve = DAILY_RESERVE_DEFAULT
        self.cache_ttl = cache_ttl
        self.clock = clock
        self._cache = {}
        self._inflight = set()
        self._lock = threading.Lock()

    # ---- audit (IDs and outcomes only - never tokens)
    @staticmethod
    def _audit_path():
        return os.getenv("ALLOCATION_AUDIT_LOG") or "allocation_removal_audit.log"

    def audit(self, entry):
        try:
            with open(self._audit_path(), "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": _now_iso(), **entry}) + "\n")
        except OSError:
            pass  # auditing must never break the operation

    def audit_stats(self, tenant_id):
        ok = failed = 0
        try:
            with open(self._audit_path(), "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if e.get("tenantId") == tenant_id:
                        ok += 1 if e.get("ok") else 0
                        failed += 0 if e.get("ok") else 1
        except OSError:
            pass
        return {"succeeded": ok, "failed": failed}

    # ---- listing
    # The list cache lives in each gunicorn worker's memory. A removal handled by worker A can only clear A's cache,
    # so worker B kept serving the old list (the "count jumped back after reload" symptom). The epoch file is the
    # shared signal: every removal stamps it, and any cache entry older than the stamp is ignored by every worker.
    @staticmethod
    def _epoch_path():
        return os.getenv("ALLOCATION_CACHE_EPOCH_FILE") or "allocation_cache_epoch"

    def _bump_epoch(self):
        try:
            with open(self._epoch_path(), "w", encoding="utf-8") as fh:
                fh.write(repr(self.clock()))
        except OSError:
            pass  # worst case: the 60 s TTL still applies

    def _epoch(self):
        try:
            with open(self._epoch_path(), "r", encoding="utf-8") as fh:
                return float(fh.read().strip())
        except (OSError, ValueError):
            return 0.0

    def invalidate(self, tenant_id):
        for key in [k for k in self._cache if k[0] == tenant_id]:
            self._cache.pop(key, None)
        self._bump_epoch()

    @staticmethod
    def _rows_from_items(module_key, mod, items):
        rows = []
        for parent in items:
            if mod["type"] and parent.get("Type") != mod["type"]:
                continue  # never rely on the server-side filter alone for the module split
            for i, alloc in enumerate(parent.get("Allocations") or []):
                if not alloc.get("IsDeleted"):
                    rows.append(normalize_row(module_key, parent, alloc, i))
        return rows

    # ---- Xero call budget (read from Xero's own response headers; nothing is guessed)
    def limits(self, ctx):
        """Last known remaining calls for this organisation, as the UI wants them; None if Xero has not told us yet."""
        getter = getattr(self.xero, "limits_for", None)
        info = getter(ctx["tenant_id"]) if getter else None
        if not info:
            return None
        return {"dayRemaining": info.get("day"), "minuteRemaining": info.get("minute"), "reserve": self.daily_reserve}

    def _guard_budget(self, ctx):
        """Stop cleanly BEFORE the daily limit instead of failing halfway. Only a recent reading counts, so a stale low
        value can never block the tool for good (after LIMIT_MAX_AGE one call is let through and refreshes it)."""
        getter = getattr(self.xero, "limits_for", None)
        info = getter(ctx["tenant_id"], LIMIT_MAX_AGE) if getter else None
        day = (info or {}).get("day")
        if day is not None and day <= self.daily_reserve:
            raise XeroApiError(429, "DAILY_BUDGET_LOW",
                               f"Stopped on purpose: Xero reports only {day} API calls left today for this organisation "
                               f"(reserve {self.daily_reserve}). Nothing was lost. Continue after the daily limit resets.")

    def fetch_page(self, ctx, module_key, f, page):
        """ONE Xero list call per HTTP request. The browser walks the pages itself (with retries and a progress bar),
        so no request can outlive gunicorn/nginx timeouts however large the organisation is. Stateless on purpose:
        nothing is cached on the server, so every worker answers the same."""
        mod = MODULES[module_key]
        if not 1 <= page <= self.fetch_max_pages:
            raise XeroApiError(400, "PAGE_OUT_OF_RANGE", f"page must be between 1 and {self.fetch_max_pages}.")
        params = {"page": page, "pageSize": PAGE_SIZE}
        where = build_where(mod, f)
        if where:
            params["where"] = where
        # rate_retries_allowed=0: on a 429 answer immediately with retryAfter instead of sleeping inside the request
        data = self.xero.request("GET", f"/{mod['resource']}", ctx["tenant_id"], params=params, rate_retries_allowed=0) or {}
        items = data.get(mod["resource"]) or []
        return {"rows": self._rows_from_items(module_key, mod, items), "page": page, "documents": len(items),
                "hasMore": len(items) >= PAGE_SIZE, "maxPages": self.fetch_max_pages, "limits": self.limits(ctx)}

    def list_rows(self, ctx, module_key, f):
        mod = MODULES[module_key]
        key = (ctx["tenant_id"], module_key, f["status"], f["from"], f["to"])
        hit = self._cache.get(key)
        if hit and not f["refresh"] and self.clock() - hit["at"] < self.cache_ttl and hit["at"] > self._epoch():
            return hit
        where = build_where(mod, f)
        rows, truncated = [], False
        for page in range(1, self.max_pages + 1):
            params = {"page": page, "pageSize": PAGE_SIZE}
            if where:
                params["where"] = where
            data = self.xero.request("GET", f"/{mod['resource']}", ctx["tenant_id"], params=params) or {}
            items = data.get(mod["resource"]) or []
            rows.extend(self._rows_from_items(module_key, mod, items))
            if len(items) < PAGE_SIZE:
                break
            if page == self.max_pages:
                truncated = True
        rows.sort(key=lambda r: r["allocationDate"] or "", reverse=True)
        entry = {"at": self.clock(), "rows": rows, "truncated": truncated}
        self._cache[key] = entry
        return entry

    @staticmethod
    def filter_rows(entry, f):
        q = f["search"].lower()
        rows = entry["rows"]
        if q:
            def hit(r):
                return any(q in str(r[k]).lower() for k in
                           ("parentNumber", "reference", "contactName", "relatedDocumentNumber", "parentId", "allocationId") if r.get(k))
            rows = [r for r in rows if hit(r)]
        return rows

    def targets(self, entry, f):
        """Every removable allocation matching the filters (all pages) - the work list for 'Remove all'."""
        rows = self.filter_rows(entry, f)
        items = [{"parentId": r["parentId"], "allocationId": r["allocationId"], "expectedAmount": r["allocationAmount"],
                  "label": r["parentNumber"] or r["reference"] or r["parentId"], "contact": r["contactName"],
                  "related": r["relatedDocumentNumber"], "amount": r["allocationAmount"], "currency": r["currency"]}
                 for r in rows if r["removable"]]
        return {"items": items, "total": len(rows), "removable": len(items), "truncated": entry["truncated"],
                "fetchedAt": datetime.fromtimestamp(entry["at"], tz=timezone.utc).isoformat()}

    @classmethod
    def query_rows(cls, entry, f):
        rows = cls.filter_rows(entry, f)
        total = len(rows)
        pages = max(1, -(-total // f["pageSize"]))
        page = min(f["page"], pages)
        start = (page - 1) * f["pageSize"]
        return {"rows": rows[start:start + f["pageSize"]], "total": total, "page": page, "pageSize": f["pageSize"],
                "totalPages": pages, "truncated": entry["truncated"],
                "fetchedAt": datetime.fromtimestamp(entry["at"], tz=timezone.utc).isoformat()}

    # ---- removal
    def _fetch_parent(self, ctx, mod, parent_id):
        try:
            data = self.xero.request("GET", f"/{mod['resource']}/{parent_id}", ctx["tenant_id"])
        except XeroApiError as exc:
            if exc.code == "XERO_NOT_FOUND":
                raise XeroApiError(404, "INVALID_PARENT_ID", f"{mod['label']} not found in this organisation.") from None
            raise
        items = (data or {}).get(mod["resource"]) or []
        if not items:
            raise XeroApiError(404, "INVALID_PARENT_ID", f"{mod['label']} not found in this organisation.")
        return items[0]

    @staticmethod
    def _find_allocation(parent, allocation_id):
        for alloc in parent.get("Allocations") or []:
            if str(alloc.get("AllocationID", "")).lower() == allocation_id.lower() and not alloc.get("IsDeleted"):
                return alloc
        return None

    @staticmethod
    def _summarize(resp):
        a = ((resp or {}).get("Allocations") or [None])[0] or (resp or {}).get("Allocation") or resp or {}
        return {"isDeleted": a.get("IsDeleted"), "amount": a.get("Amount")}

    def _check_parent(self, mod, parent):
        if mod["type"] and parent.get("Type") != mod["type"]:
            other = "Bill Credit" if parent.get("Type") == "ACCPAYCREDIT" else "Credit Note"
            raise XeroApiError(422, "WRONG_MODULE", f"This document is of type {parent.get('Type')}. Use the {other} module instead.")
        if parent.get("Status") in BLOCKED_STATUSES:
            raise XeroApiError(422, "UNSUPPORTED_STATUS", f"Allocations cannot be removed while the document status is {parent.get('Status')}.")

    def _check_allocation(self, parent, payload, out):
        alloc = self._find_allocation(parent, payload["allocationId"])
        if alloc is None:
            raise XeroApiError(404, "ALLOCATION_NOT_FOUND", "Allocation not found on this document. It may already have been removed. No request was sent to Xero.")
        out["amount"] = allocation_amount(alloc)
        expected = payload.get("expectedAmount")
        if expected is not None:
            try:
                changed = float(expected) != float(out["amount"])
            except (TypeError, ValueError):
                changed = True
            if changed:
                raise XeroApiError(409, "STALE_DATA", f"The allocation amount in Xero ({out['amount']}) differs from what you selected ({expected}). Refresh and review again.")

    def _delete_one(self, ctx, mod, parent_id, allocation_id, out):
        """The DELETE itself (allocation endpoint only). An unclear outcome is checked against the real state first."""
        path = f"/{mod['resource']}/{parent_id}/Allocations/{allocation_id}"
        attempt = 0
        while True:
            attempt += 1
            try:
                out["xeroResponse"] = self._summarize(self.xero.request("DELETE", path, ctx["tenant_id"]))
                return
            except XeroApiError as exc:
                if not exc.ambiguous:
                    raise  # definite rejection: Xero changed nothing
                # Outcome unknown (timeout / 5xx / network): look at the real state BEFORE any retry.
                try:
                    current = self._fetch_parent(ctx, mod, parent_id)
                except XeroApiError:
                    raise XeroApiError(502, "OUTCOME_UNKNOWN", "The request failed and the current state could not be checked. Refresh and verify in Xero before retrying.") from None
                if self._find_allocation(current, allocation_id) is None:
                    out["xeroResponse"] = {"isDeleted": None, "amount": None, "note": "Request errored but the allocation is gone (processed by Xero)."}
                    return
                if attempt >= 2:
                    raise
                # still present -> exactly one safe retry

    def _verify_gone(self, after, allocation_id, out):
        if self._find_allocation(after, allocation_id) is not None:
            raise XeroApiError(502, "VERIFICATION_FAILED", "Xero responded but the allocation still appears on the document. It was NOT confirmed as removed.")
        out["verification"] = {"documentStillExists": True, "allocationPresentAfter": False, "documentStatusAfter": after.get("Status")}

    def _do_remove(self, ctx, mod, payload, out):
        parent_id, allocation_id = payload["parentId"], payload["allocationId"]
        self._guard_budget(ctx)
        # 1. Pre-flight
        parent = self._fetch_parent(ctx, mod, parent_id)
        out["parentNumber"] = parent.get("CreditNoteNumber")
        out["contactName"] = (parent.get("Contact") or {}).get("Name")
        self._check_parent(mod, parent)
        self._check_allocation(parent, payload, out)
        # 2. DELETE - the allocation endpoint only
        self._delete_one(ctx, mod, parent_id, allocation_id, out)
        # 3. Verify by re-fetching
        try:
            after = self._fetch_parent(ctx, mod, parent_id)
        except XeroApiError:
            raise XeroApiError(502, "VERIFICATION_FAILED", "Xero accepted the request but the verification re-fetch failed. Check the document in Xero.") from None
        self._verify_gone(after, allocation_id, out)

    # ---- result helpers (shared by single and grouped removal)
    @staticmethod
    def _new_out(module_key, item):
        item = item if isinstance(item, dict) else {}
        return {"ok": False, "module": module_key, "parentId": item.get("parentId"), "allocationId": item.get("allocationId"),
                "processedAt": _now_iso()}

    @staticmethod
    def _set_failed(out, exc):
        out.update(ok=False, status="UNVERIFIED" if exc.code in ("VERIFICATION_FAILED", "OUTCOME_UNKNOWN") else "FAILED",
                   errorCode=exc.code, message=exc.message, httpStatus=exc.status)
        if getattr(exc, "retry_after", None):
            out["retryAfter"] = exc.retry_after
        return out

    @staticmethod
    def _set_skipped(out, code, message, retry_after=None):
        out.update(ok=False, status="SKIPPED", errorCode=code, message=message)
        if retry_after:
            out["retryAfter"] = retry_after
        return out

    @staticmethod
    def _set_verified(out):
        out.update(ok=True, status="REMOVED_VERIFIED", httpStatus=200,
                   message="Allocation removed and verified by re-fetching the document. The document itself was not deleted.")
        return out

    @staticmethod
    def _id_error(mod, item):
        if not is_uuid(item.get("parentId")):
            return XeroApiError(400, "INVALID_PARENT_ID", f"parentId must be a valid {mod['label']} ID (GUID).")
        if not is_uuid(item.get("allocationId")):
            return XeroApiError(400, "INVALID_ALLOCATION_ID", "allocationId must be a valid Allocation ID (GUID).")
        return None

    def _audit_result(self, ctx, module_key, out):
        self.audit({"tenantId": ctx["tenant_id"], "module": module_key, "parentId": out["parentId"],
                    "allocationId": out["allocationId"], "amount": out.get("amount"), "ok": out["ok"],
                    "status": out.get("status"), "errorCode": out.get("errorCode")})

    def remove(self, ctx, module_key, payload):
        """Never raises for expected failures: returns a result dict (so a bulk run can collect per-item results)."""
        mod = MODULES[module_key]
        payload = payload if isinstance(payload, dict) else {}
        parent_id, allocation_id = payload.get("parentId"), payload.get("allocationId")
        out = self._new_out(module_key, payload)

        err = self._id_error(mod, payload)
        if err:
            return self._set_failed(out, err)

        lock_key = f"{ctx['tenant_id']}:{allocation_id.lower()}"
        with self._lock:
            if lock_key in self._inflight:
                return self._set_failed(out, XeroApiError(409, "DUPLICATE_IN_PROGRESS", "A removal for this allocation is already in progress."))
            self._inflight.add(lock_key)
        try:
            self._do_remove(ctx, mod, payload, out)
            self._set_verified(out)
        except XeroApiError as exc:
            self._set_failed(out, exc)
        except Exception as exc:  # noqa: BLE001 - last resort, never leak details
            log.error("unexpected removal error (%s)", type(exc).__name__)
            self._set_failed(out, XeroApiError(500, "INTERNAL_ERROR", "Unexpected error while removing the allocation."))
        finally:
            with self._lock:
                self._inflight.discard(lock_key)
            self.invalidate(ctx["tenant_id"])
        self._audit_result(ctx, module_key, out)
        return out

    def remove_group(self, ctx, module_key, parent_id, items, deadline=None):
        """Remove several allocations of ONE document with 1 pre-flight + N DELETEs + 1 verification (N + 2 Xero calls
        instead of 3N). Every allocation is still checked against the freshly fetched document, still sent as its own
        DELETE, and still confirmed by re-fetching the document afterwards. Same result shape as remove(); results are
        returned in the order of `items`. deadline: clock() value after which the not-yet-started allocations are
        SKIPPED/TIME_BUDGET (nothing sent to Xero for them)."""
        mod = MODULES[module_key]
        outs, active = [], []
        for item in items:
            item = item if isinstance(item, dict) else {}
            outs.append(self._new_out(module_key, item))
        locked = []
        try:
            for item, out in zip(items, outs):
                err = self._id_error(mod, item) if isinstance(item, dict) else XeroApiError(400, "INVALID_PARENT_ID", "Invalid item.")
                if err is None and str(item["parentId"]).lower() != str(parent_id).lower():
                    err = XeroApiError(400, "INVALID_PARENT_ID", "Allocation does not belong to this document.")
                if err:
                    self._set_failed(out, err)
                    continue
                lock_key = f"{ctx['tenant_id']}:{item['allocationId'].lower()}"
                with self._lock:
                    if lock_key in self._inflight:
                        self._set_failed(out, XeroApiError(409, "DUPLICATE_IN_PROGRESS", "A removal for this allocation is already in progress."))
                        continue
                    self._inflight.add(lock_key)
                locked.append(lock_key)
                active.append((item, out))
            if active:
                try:
                    self._run_group(ctx, mod, module_key, parent_id, active, deadline)
                except Exception as exc:  # noqa: BLE001 - last resort, never leak details
                    log.error("unexpected removal error (%s)", type(exc).__name__)
                    for _, out in active:
                        if not out.get("status"):
                            self._set_failed(out, XeroApiError(500, "INTERNAL_ERROR", "Unexpected error while removing the allocation."))
        finally:
            with self._lock:
                for key in locked:
                    self._inflight.discard(key)
            self.invalidate(ctx["tenant_id"])
        for out in outs:
            if out.get("status") != "SKIPPED":
                self._audit_result(ctx, module_key, out)
        return outs

    def _run_group(self, ctx, mod, module_key, parent_id, active, deadline):
        # 1. ONE pre-flight for the whole document
        try:
            self._guard_budget(ctx)
            parent = self._fetch_parent(ctx, mod, parent_id)
            self._check_parent(mod, parent)
        except XeroApiError as exc:
            if exc.code in FATAL_CODES or exc.code == "RATE_LIMITED":
                # Same rule as a sequential run: the first allocation reports the error, the rest are not started.
                for n, (_, out) in enumerate(active):
                    if n == 0:
                        self._set_failed(out, exc)
                    else:
                        self._set_skipped(out, exc.code, "Skipped because an earlier item hit a connection or limit error.", exc.retry_after)
            else:
                for _, out in active:
                    self._set_failed(out, exc)
            return
        number, contact = parent.get("CreditNoteNumber"), (parent.get("Contact") or {}).get("Name")
        # 2. one DELETE per allocation
        deleted, abort, started = [], None, False
        for item, out in active:
            out["parentNumber"], out["contactName"] = number, contact
            if abort:
                self._set_skipped(out, abort[0], "Skipped because an earlier item hit a connection or limit error.", abort[1])
                continue
            if started and deadline is not None and self.clock() >= deadline:
                self._set_skipped(out, "TIME_BUDGET", "Not started yet (request time budget reached). Send again.")
                continue
            started = True
            try:
                self._check_allocation(parent, item, out)
                self._guard_budget(ctx)
                self._delete_one(ctx, mod, parent_id, item["allocationId"], out)
                deleted.append((item, out))
            except XeroApiError as exc:
                self._set_failed(out, exc)
                if exc.code in FATAL_CODES or exc.code == "RATE_LIMITED":
                    abort = (exc.code, exc.retry_after)
        # 3. ONE verification for everything that was deleted
        if not deleted:
            return
        try:
            after = self._fetch_parent(ctx, mod, parent_id)
        except XeroApiError:
            for _, out in deleted:
                self._set_failed(out, XeroApiError(502, "VERIFICATION_FAILED", "Xero accepted the request but the verification re-fetch failed. Check the document in Xero."))
            return
        for item, out in deleted:
            try:
                self._verify_gone(after, item["allocationId"], out)
                self._set_verified(out)
            except XeroApiError as exc:
                self._set_failed(out, exc)

    def remove_bulk(self, ctx, module_key, items, budget=None, max_items=MAX_BULK):
        """Allocations of the same document are removed together (see remove_group), which saves Xero API calls.
        budget (seconds): once exceeded, the remaining items are returned as SKIPPED/TIME_BUDGET (nothing was sent
        to Xero for them) so the caller can send them again in the next request. Results keep the order of `items`."""
        if not isinstance(items, list) or not items:
            raise XeroApiError(400, "NO_ITEMS", "Select at least one allocation.")
        if len(items) > max_items:
            raise XeroApiError(400, "TOO_MANY_ITEMS", f"At most {max_items} allocations per request.")
        mod = MODULES[module_key]
        results, seen, groups = [None] * len(items), set(), {}
        aborted, abort_retry = None, None
        started = self.clock()
        deadline = None if budget is None else started + budget

        def skipped(item, code, message):
            return self._set_skipped(self._new_out(module_key, item), code, message)

        # Group by document, in order of first appearance. Anything invalid stays on its own (it fails fast, no Xero call).
        for i, item in enumerate(items):
            alloc_id = str((item or {}).get("allocationId") or "").lower() if isinstance(item, dict) else ""
            if alloc_id and alloc_id in seen:
                results[i] = skipped(item, "DUPLICATE_IN_REQUEST", "Duplicate allocation in this request.")
                continue
            if alloc_id:
                seen.add(alloc_id)
            valid = isinstance(item, dict) and self._id_error(mod, item) is None
            key = str(item["parentId"]).lower() if valid else ("single", i)
            groups.setdefault(key, []).append(i)

        processed_any = False
        for key, idxs in groups.items():
            if aborted:
                for i in idxs:
                    res = skipped(items[i], aborted, "Skipped because an earlier item hit a connection or limit error.")
                    if abort_retry:
                        res["retryAfter"] = abort_retry
                    results[i] = res
                continue
            if deadline is not None and processed_any and self.clock() >= deadline:
                for i in idxs:
                    results[i] = skipped(items[i], "TIME_BUDGET", "Not started yet (request time budget reached). Send again.")
                continue
            processed_any = True
            if len(idxs) == 1:
                group_results = [self.remove(ctx, module_key, items[idxs[0]])]
            else:
                group_results = self.remove_group(ctx, module_key, items[idxs[0]]["parentId"], [items[i] for i in idxs], deadline)
            for i, res in zip(idxs, group_results):
                results[i] = res
            for res in group_results:
                if not res["ok"] and res.get("status") != "SKIPPED" and (res.get("errorCode") in FATAL_CODES or res.get("errorCode") == "RATE_LIMITED"):
                    aborted, abort_retry = res["errorCode"], res.get("retryAfter")   # RATE_LIMITED: stop the chunk; nothing was processed, the caller waits and resends
                    break
        ok = sum(1 for r in results if r["ok"])
        skip = sum(1 for r in results if r.get("status") == "SKIPPED")
        return {"results": results, "summary": {"total": len(results), "succeeded": ok, "failed": len(results) - ok - skip, "skipped": skip},
                "limits": self.limits(ctx)}


# ------------------------------------------------------------------------------------------- blueprint
def _err(status, code, message, retry_after=None):
    body = {"ok": False, "errorCode": code, "message": message}
    if retry_after:
        body["retryAfter"] = retry_after
    return jsonify(body), status


def register(app, *, get_valid_token, force_refresh, get_tenant_info):
    try:
        ttl = int(os.getenv("ALLOCATION_LIST_CACHE_SECONDS", "600"))
    except ValueError:
        ttl = 600
    # Paging / searching a fetched list is served from memory; a removal clears it (see _bump_epoch).
    try:
        inline_wait = float(os.getenv("XERO_MAX_INLINE_WAIT", "20"))
    except ValueError:
        inline_wait = 20.0
    # A rate-limit wait longer than this is not slept inside the HTTP request (it would risk the worker timeout);
    # the browser waits and resends instead.
    service = AllocationService(XeroClient(get_valid_token, force_refresh, max_retry_wait=inline_wait), cache_ttl=ttl)
    app.extensions["allocation_removal"] = service
    bp = Blueprint("allocations", __name__, url_prefix="/allocations")

    @bp.before_request
    def gate():
        if not flag_on():
            return _err(404, "FEATURE_DISABLED", "Allocation removal is disabled.")
        if not secret_configured():
            return _err(503, "SECRET_KEY_REQUIRED", "Set a strong SECRET_KEY in the environment to use allocation removal.")
        return None

    @bp.errorhandler(XeroApiError)
    def handle_api_error(exc):
        return _err(exc.status, exc.code, exc.message, retry_after=exc.retry_after)

    def csrf_token():
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return token

    def check_csrf():
        sent = request.headers.get("X-CSRF-Token", "")
        expected = session.get("csrf_token") or ""
        if not expected or not hmac.compare_digest(sent.encode(), expected.encode()):
            raise XeroApiError(403, "CSRF_REJECTED", "Missing or invalid CSRF token. Reload the page and try again.")

    def context():
        info = get_tenant_info()
        if not info:
            raise XeroApiError(401, "NOT_CONNECTED", "Connect to Xero first.")
        return {"tenant_id": info["tenantId"], "tenant_name": info.get("tenantName")}

    def module_or_404(key):
        if key not in MODULES:
            raise XeroApiError(404, "UNKNOWN_MODULE", "Unknown module.")
        return key

    def write_guard(module):
        """Everything a destructive call must pass BEFORE any Xero request is made."""
        module_or_404(module)
        check_csrf()
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise XeroApiError(400, "BAD_JSON", "A JSON body is required.")
        if body.get("confirm") is not True:
            raise XeroApiError(400, "CONFIRMATION_REQUIRED", "Explicit confirmation is required.")
        ctx = context()
        if not tenant_allowed(ctx["tenant_id"]):
            raise XeroApiError(403, "TENANT_NOT_ALLOWED", "Removal is not enabled for this organisation (REMOVAL_ALLOWED_TENANT_IDS).")
        return body, ctx

    @bp.get("/")
    def page():
        info = get_tenant_info()
        return render_template("allocations.html", org_name=(info or {}).get("tenantName"), connected=bool(info),
                               csrf_token=csrf_token(),
                               removal_blocked=bool(info) and not tenant_allowed(info["tenantId"]))

    @bp.get("/api/status")
    def status():
        ctx = context()
        return jsonify({"organisation": ctx["tenant_name"], "removalAllowed": tenant_allowed(ctx["tenant_id"]),
                        "stats": service.audit_stats(ctx["tenant_id"]), "limits": service.limits(ctx)})

    @bp.get("/api/<module>/list")
    def list_allocations(module):
        module_or_404(module)
        ctx = context()
        f = parse_list_query(request.args)
        return jsonify(service.query_rows(service.list_rows(ctx, module, f), f))

    @bp.post("/api/<module>/remove")
    def remove_one(module):
        body, ctx = write_guard(module)
        result = service.remove(ctx, module, body)
        return jsonify(result), 200 if result["ok"] else result.get("httpStatus", 500)

    @bp.get("/api/<module>/page")
    def fetch_one_page(module):
        """Read-only: one page of Xero documents, already split into allocation rows. The browser loops over pages."""
        module_or_404(module)
        ctx = context()
        f = parse_list_query(request.args)
        return jsonify(service.fetch_page(ctx, module, f, f["page"]))

    @bp.get("/api/<module>/targets")
    def list_targets(module):
        """Read-only: the complete work list for 'Remove all' (every removable row matching the filters)."""
        module_or_404(module)
        ctx = context()
        f = parse_list_query(request.args)
        return jsonify(service.targets(service.list_rows(ctx, module, f), f))

    @bp.post("/api/<module>/remove-bulk")
    def remove_many(module):
        body, ctx = write_guard(module)
        if body.get("auto") is True:  # one chunk of a "Remove all" run: bigger, but time-boxed
            return jsonify(service.remove_bulk(ctx, module, body.get("items"), budget=AUTO_BUDGET_SECONDS, max_items=MAX_AUTO))
        return jsonify(service.remove_bulk(ctx, module, body.get("items")))

    app.register_blueprint(bp)
    return service
