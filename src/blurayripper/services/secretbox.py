"""Credentials shared through Postgres, encrypted with a passphrase on the box.

WHAT THIS PROTECTS AGAINST, precisely, because encryption invites the assumption
that everything is now safe:

Postgres runs on a different machine from the rippers. Moving credentials there
so they stop being lost in migrations would otherwise mean a database dump, a
replica, or anyone with database access handing over every API key in plaintext.
Encrypting them with a passphrase that lives ONLY in each box's secrets.env
means the database holds ciphertext and nothing else — a leaked dump is inert.

WHAT IT DOES NOT PROTECT AGAINST: anyone who can read secrets.env on a ripper.
They have the passphrase, so they have the keys. That is unavoidable — the box
must be able to decrypt unattended, at boot, with nobody there to type anything.

This is why BLURAYRIPPER_DATABASE_URL stays in secrets.env in the clear.
"Encrypting" it with a key stored in the same file protects nothing: whoever can
read one line can read the other. That would be obfuscation dressed as security,
and the point of writing it down here is so nobody later mistakes its absence
for an oversight.

The per-box values (tool paths, staging/output folders) deliberately do not move
either. They differ per machine — the Windows worker's Z:\\ paths are not the
Linux box's /mnt paths — so sharing them would break the cluster, not save it.
"""

from __future__ import annotations

import base64
import json
import os

# Only these move. An allowlist, not a denylist: a new secrets.env key must be
# considered deliberately rather than being shared by accident.
SHARED_KEYS: tuple[str, ...] = (
    "TMDB_API_KEY",
    "OPENSUBTITLES_API_KEY",
    "OPENSUBTITLES_USERNAME",
    "OPENSUBTITLES_PASSWORD",
    "BLURAYRIPPER_NOTIFY_URL",   # webhook URLs usually embed a token
)

# The passphrase itself, read from secrets.env like any other local value.
PASSPHRASE_KEY = "BLURAYRIPPER_SECRETS_KEY"
# cluster_config key holding the encrypted blob.
SHARED_KEY = "secrets.v1"

# scrypt, sized so an offline guess against a leaked dump is expensive while a
# once-per-process derivation stays unnoticeable (~0.1s).
_KDF = {"n": 1 << 15, "r": 8, "p": 1, "dklen": 32}


class SecretsUnavailable(RuntimeError):
    """Raised when a blob exists but cannot be opened — never silently ignored,
    because falling back to "no credentials" looks identical to a working setup
    with nothing configured, and that is how a wrong passphrase would go
    unnoticed until artwork quietly stopped downloading."""


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise SecretsUnavailable(
            "Shared secrets need the 'cryptography' package. Install it with "
            "`pip install -e .[secrets]`, or unset "
            f"{PASSPHRASE_KEY} to keep credentials in secrets.env on each box."
        ) from exc
    return AESGCM


def derive_key(passphrase: str, salt: bytes, params: dict | None = None) -> bytes:
    """scrypt. Params travel with the blob so they can be raised later without
    stranding anything already encrypted."""
    import hashlib

    p = {**_KDF, **(params or {})}
    return hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt,
        n=int(p["n"]), r=int(p["r"]), p=int(p["p"]), dklen=int(p["dklen"]),
        maxmem=(1 << 26) + (1 << 25),
    )


def encrypt(passphrase: str, values: dict[str, str]) -> dict:
    """Encrypt {name: value} into a JSON-safe blob for cluster_config."""
    if not passphrase:
        raise SecretsUnavailable(f"{PASSPHRASE_KEY} is not set.")
    AESGCM = _aesgcm()
    salt, nonce = os.urandom(16), os.urandom(12)
    key = derive_key(passphrase, salt)
    plaintext = json.dumps(values, sort_keys=True).encode("utf-8")
    ct = AESGCM(key).encrypt(nonce, plaintext, None)
    return {"v": 1, "kdf": "scrypt", "params": dict(_KDF),
            "salt": _b64(salt), "nonce": _b64(nonce), "ct": _b64(ct)}


def decrypt(passphrase: str, blob: dict) -> dict[str, str]:
    """Open a blob. Raises SecretsUnavailable on a wrong passphrase or tampering."""
    if not passphrase:
        raise SecretsUnavailable(
            f"The cluster has stored credentials but {PASSPHRASE_KEY} is not set "
            "in this box's config/secrets.env, so they cannot be decrypted.")
    if not isinstance(blob, dict) or blob.get("v") != 1:
        raise SecretsUnavailable(f"Unrecognised secrets blob (version {blob!r}).")
    AESGCM = _aesgcm()
    try:
        key = derive_key(passphrase, _unb64(blob["salt"]), blob.get("params"))
        raw = AESGCM(key).decrypt(_unb64(blob["nonce"]), _unb64(blob["ct"]), None)
    except SecretsUnavailable:
        raise
    except Exception as exc:
        raise SecretsUnavailable(
            f"Could not decrypt the cluster's credentials ({type(exc).__name__}). "
            f"The usual cause is {PASSPHRASE_KEY} differing from the box that "
            "stored them — it must be identical everywhere.") from exc
    values = json.loads(raw.decode("utf-8"))
    return {k: str(v) for k, v in values.items() if isinstance(k, str)}


def movable(secrets: dict[str, str]) -> dict[str, str]:
    """The subset of a secrets.env dict that is safe to share, non-empty only."""
    return {k: secrets[k] for k in SHARED_KEYS
            if secrets.get(k, "").strip()}


def fetch(store, passphrase: str) -> dict[str, str]:
    """Decrypted cluster credentials, or {} when none are stored.

    A store that is unreachable returns {} — the box then runs on whatever is in
    its own secrets.env, which is the pre-existing behaviour. A blob that exists
    but will not open raises, because that is a misconfiguration to fix, not a
    condition to degrade through.
    """
    if store is None or not getattr(store, "durable", False):
        return {}
    try:
        blob = store.get_shared_config(SHARED_KEY)
    except Exception:
        return {}
    return decrypt(passphrase, blob) if blob else {}


def publish(store, passphrase: str, values: dict[str, str]) -> int:
    """Encrypt and store `values`. Returns how many were stored."""
    if store is None or not getattr(store, "durable", False):
        raise SecretsUnavailable("Sharing credentials needs a Postgres store.")
    store.set_shared_config(SHARED_KEY, encrypt(passphrase, values))
    return len(values)
