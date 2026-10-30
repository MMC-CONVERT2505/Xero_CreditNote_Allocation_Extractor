"""
Token persistence for the Xero tool.

* If TOKEN_ENCRYPTION_KEY is set the token file is stored encrypted (Fernet, from the `cryptography` package that is
  already pinned in requirements.txt). Any passphrase works: it is stretched into a 32-byte key with scrypt (stdlib).
* If it is not set the file stays plain JSON exactly as before (backward compatible) - but it is now written
  atomically and with owner-only permissions.
* A legacy plain-text file is migrated to the encrypted form automatically the first time it is read while a key
  is configured. No reconnect is needed.
* A file that cannot be decrypted is NEVER overwritten by a read; TokenStoreError is raised instead.
* A cross-thread and (best effort) cross-process lock lets callers make "check expiry -> refresh -> save" atomic,
  so two workers do not both spend the same single-use refresh token.
"""
import base64
import contextlib
import functools
import hashlib
import json
import os
import tempfile
import threading

ENC_PREFIX = "enc:v1:"
_SALT = b"xero-allocation-tool:token-store:v1"


class TokenStoreError(Exception):
    """Raised when the token file cannot be read safely. The message never contains token material."""


@functools.lru_cache(maxsize=4)
def _derive_key(secret):
    raw = hashlib.scrypt(secret.encode("utf-8"), salt=_SALT, n=2 ** 14, r=8, p=1, dklen=32)
    return base64.urlsafe_b64encode(raw)


def encryption_enabled():
    return bool(os.getenv("TOKEN_ENCRYPTION_KEY"))


def _fernet():
    secret = os.getenv("TOKEN_ENCRYPTION_KEY")
    if not secret:
        return None
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - cryptography is in requirements.txt
        raise TokenStoreError("TOKEN_ENCRYPTION_KEY is set but the 'cryptography' package is not installed.") from exc
    return Fernet(_derive_key(secret))


# --------------------------------------------------------------------------------------------- locking
_thread_lock = threading.RLock()
_local = threading.local()


def _acquire_os_lock(lock_path):
    try:
        fh = open(lock_path, "a+b")
    except OSError:
        return None
    try:
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except ImportError:  # Windows
            import msvcrt
            fh.seek(0)
            if not fh.read(1):
                fh.write(b"0")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
    except OSError:
        pass  # best effort: the in-process lock still applies
    return fh


def _release_os_lock(fh):
    try:
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except ImportError:  # Windows
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        fh.close()


@contextlib.contextmanager
def locked(path):
    """Re-entrant lock around the token file (threads + best-effort across processes)."""
    with _thread_lock:
        depth = getattr(_local, "depth", 0)
        handle = _acquire_os_lock(path + ".lock") if depth == 0 else None
        _local.depth = depth + 1
        try:
            yield
        finally:
            _local.depth = depth
            if handle is not None:
                _release_os_lock(handle)


# --------------------------------------------------------------------------------------------- read / write
def _write(path, data, fernet):
    if fernet is None:
        payload = json.dumps(data, indent=2)  # identical format to the original implementation
    else:
        payload = ENC_PREFIX + fernet.encrypt(json.dumps(data).encode("utf-8")).decode("ascii")
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".xero_tokens.", suffix=".tmp", dir=directory)  # created 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def save(path, data):
    with locked(path):
        _write(path, data, _fernet())


def load(path):
    """Return the stored token dict, or None if there is no (or an empty) file."""
    with locked(path):
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read().strip()
        if not raw:
            return None
        fernet = _fernet()
        if raw.startswith(ENC_PREFIX):
            if fernet is None:
                raise TokenStoreError("The token file is encrypted but TOKEN_ENCRYPTION_KEY is not set.")
            try:
                return json.loads(fernet.decrypt(raw[len(ENC_PREFIX):].encode("ascii")).decode("utf-8"))
            except Exception:  # InvalidToken, bad base64, bad JSON ...
                raise TokenStoreError(
                    "The token file could not be decrypted. Check TOKEN_ENCRYPTION_KEY. The file was not modified."
                ) from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise TokenStoreError("The token file is not valid JSON.") from None
        if fernet is not None:  # legacy plain-text file -> migrate in place
            _write(path, data, fernet)
        return data
