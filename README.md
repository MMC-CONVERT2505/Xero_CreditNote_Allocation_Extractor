# Xero Credit Note & Overpayment Allocation Extractor

Flask tool that extracts credit-note / overpayment allocations to Excel, plus an optional, **disabled-by-default**
Allocation Removal page (Credit Notes, Bill Credits `ACCPAYCREDIT`, Overpayments).

## Run
    pip install -r requirements.txt     # unchanged - no new dependency
    python app.py

Env vars: see `.env.example`.

## Allocation Removal (off by default)
Enable only after testing on a Xero **demo company**:

    ENABLE_ALLOCATION_REMOVAL=true
    SECRET_KEY=<long random string>        # mandatory, otherwise the page answers 503
    TOKEN_ENCRYPTION_KEY=<passphrase>      # recommended
    REMOVAL_ALLOWED_TENANT_IDS=<demo org tenant id>   # recommended while testing

Then open `/allocations/` (a button appears on the home page). Flow: list -> select -> confirmation modal
(tick acknowledgement) -> pre-flight check -> `DELETE .../Allocations/{AllocationID}` -> verify by re-fetch -> audit log.
Only the allocation link is removed; the credit note / overpayment / invoice is never deleted.

Safety: CSRF token + `confirm: true` on every write, random single-use OAuth `state`, tokens never in the browser
session or logs, encrypted token file, no blind retry after timeouts (state is re-checked first), 429 `Retry-After` honoured,
duplicate requests rejected, bulk max 50.

**There is no login on this app itself.** Put it behind VPN / nginx basic auth before enabling removal on a server.

## Tests
    python -m unittest discover -s tests -t . -v      # stdlib only, all Xero calls mocked

## Verified vs NOT verified
Verified by mocked tests: all three modules, error/timeout/rate-limit paths, CSRF, OAuth state, token encryption, original behaviour (golden tests).
**Not verified against real Xero (you must test on a demo company):** that list responses include `AllocationID`; actual DELETE
under the `accounting.transactions` scope; lock-date / reconciled-payment restrictions; `where`/paging on Overpayments;
exact 403 body and rate-limit headers; Windows file locking; sub-path reverse-proxy deployment.
