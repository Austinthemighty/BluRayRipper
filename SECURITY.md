# Security

## Reporting

Open a GitHub issue for anything that isn't sensitive. For something that
shouldn't be public, use GitHub's **private vulnerability reporting** on this
repository (Security → Report a vulnerability).

This is a self-hosted tool for a home media library. It has no multi-user model
and no authentication — see below.

## The web UI has no authentication

The dashboard and its API are unauthenticated by design, on the assumption that
they're bound to a trusted network. `--host 0.0.0.0` puts an interface on your
LAN that can start rips, change settings, delete jobs and restart the server, and
anyone who can reach the port can use it.

If you need it from outside, put it behind something that authenticates — a
reverse proxy with auth, a VPN, or an overlay network like Tailscale or ZeroTier.
Don't port-forward it.

## Where credentials live

Two files, both gitignored, neither ever committed:

- **`config/secrets.env`** — the database URL and the secrets passphrase. Two
  lines on a clustered box; everything else comes from the database.
- **`/etc/cifs-truenas.cred`** (or equivalent) — the NAS password, mode 600 and
  root-owned, referenced from `/etc/fstab` by `credentials=`. Never inline in
  `fstab`, which is world-readable.

API keys (TMDB, OpenSubtitles) can be shared through Postgres **encrypted at
rest**: scrypt-derived key, AES-GCM, passphrase held per box in `secrets.env`
(`services/secretbox.py`). `blurayripper secrets status` lists which credentials
are stored and never prints a value.

Two things this deliberately does **not** do:

- The **database URL is stored in the clear**, next to the passphrase that
  decrypts everything else. Encrypting it with a key kept beside it would be
  obfuscation, not security — the box has to decrypt unattended at boot, so
  whatever it can read, an attacker with the same file access can read.
- Encryption protects the **database** — its dumps, its replicas, its backups —
  from holding readable credentials. It does not protect a box whose filesystem
  is already compromised.

## Generating the passphrase

Generate it on your own machine and don't paste it anywhere:

```bash
openssl rand -base64 36
```

Every box in a cluster needs the same value. Losing it means re-issuing your
TMDB and OpenSubtitles keys, so keep a copy in a password manager.
