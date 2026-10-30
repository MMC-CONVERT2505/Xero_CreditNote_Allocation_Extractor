# Changes

## New dependency: NONE
`requirements.txt` untouched (uses `requests`, Flask, `cryptography` already pinned). Tests use stdlib `unittest`. CI/CD install unchanged.

## Modified
- `app.py` (diff in `app.py.diff`): random single-use OAuth `state` (validated, constant-time); tokens no longer stored in the session cookie;
  token file via `token_store` (optional encryption, atomic write, locked single-flight refresh, 30s timeout);
  env-overridable `XERO_REDIRECT_URI` / `XERO_SCOPES` / `XERO_TOKEN_FILE` (defaults = old values); `SECRET_KEY` env (old value remains fallback);
  cookie `HttpOnly`/`SameSite=Lax`; registers the removal blueprint.
- `templates/index.html` (diff in `index.html.diff`): one block shown only when removal is enabled; page is byte-identical otherwise.

## Added
`token_store.py`, `xero_api.py`, `allocation_removal.py`, `templates/allocations.html`, `tests/*`, `.env.example`, `.gitignore`, README.

## Unchanged on purpose (known risks)
`GET /reset` (CSRF-able), `debug=True`, no app-level login. Fetching, extraction, Excel export untouched.

## Rollback note
After enabling `TOKEN_ENCRYPTION_KEY` the token file is encrypted; reverting to the old code requires reconnecting to Xero.
