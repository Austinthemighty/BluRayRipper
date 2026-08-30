# Setting up a cluster

How to bring up a BluRayRipper cluster from nothing: one **rip box** (`serve`)
and any number of **encode workers**. A single all-in-one machine is the same
thing with the worker steps skipped.

Written from an actual two-box build. Where something is easy to get wrong, this
says what the failure looks like rather than only what to type — most of the
troubleshooting section is things that went wrong on the way.

---

## What you are building

```
   ┌──────────────┐         claims jobs        ┌──────────────────┐
   │  rip box     │ ─────────────────────────► │  encode worker   │
   │  `serve`     │                            │  `worker`        │
   │  + web UI    │ ◄───── progress, state ─── │  (any number)    │
   └──────┬───────┘                            └────────┬─────────┘
          │                                             │
          │         ┌────────────────────┐              │
          └────────►│  Postgres          │◄─────────────┘
          │         │  queue + config    │              │
          │         │  + credentials     │              │
          │         └────────────────────┘              │
          │                                             │
          │         ┌────────────────────┐              │
          └────────►│  NAS (SMB/CIFS)    │◄─────────────┘
                    │  staging + output  │
                    └────────────────────┘
```

The rip box reads discs and writes rips to **shared staging**. Workers claim
queued jobs, encode from that same staging, and place finished files in the
**shared output**. Postgres carries the queue, the settings, and the API
credentials.

Two consequences worth internalising, because most cluster faults trace back to
one of them:

- **Every box must see the same storage at the same path.** A worker resolves a
  job's ripped file under *its own* `staging_dir`. If that isn't the same
  directory the rip box wrote to, it claims jobs, finds nothing, and hands them
  back.
- **Postgres holds the configuration**, so a new box needs almost nothing local.
  Its `secrets.env` is two lines.

---

## Before you start

- **Postgres** reachable from every box (any host — it does not need to be one
  of them).
- **A NAS share** every box can mount read/write at the same path.
- **Ubuntu 22.04+ / Debian** on each box. Other distros work; you install the
  external tools yourself.
- **A MakeMKV licence** on the rip box only. Workers never rip.

---

## Part 1 — shared infrastructure (once)

### Postgres

```sql
CREATE USER bluray WITH PASSWORD 'a-strong-password';
CREATE DATABASE blurayripper OWNER bluray;
```

The schema is created later by `blurayripper db-init` from the rip box.

### Pick a secrets passphrase

Generate it once. It encrypts the API credentials stored in Postgres, and every
box needs the identical value:

```bash
openssl rand -base64 36
```

**Save it in a password manager now.** Once the credentials only exist encrypted
in Postgres, losing this means re-issuing your TMDB and OpenSubtitles keys.

---

## Part 2 — every box

### 1. Mount the NAS

Do this **first**. Several later steps check that storage is writable.

```bash
sudo apt install -y cifs-utils && sudo mkdir -p /mnt/TrueNas
sudo install -m 600 /dev/null /etc/cifs-truenas.cred
sudo nano /etc/cifs-truenas.cred
```

```
username=YOUR_NAS_USER
password=YOUR_NAS_PASSWORD
domain=WORKGROUP
```

Then one line in `/etc/fstab` — substitute your server and share:

```
//192.168.1.10/Share  /mnt/TrueNas  cifs  credentials=/etc/cifs-truenas.cred,uid=YOURUSER,gid=YOURUSER,file_mode=0664,dir_mode=0775,iocharset=utf8,nobrl,_netdev,nofail,hard,x-systemd.automount,x-systemd.mount-timeout=30  0 0
```

```bash
sudo systemctl daemon-reload && sudo mount -a && findmnt /mnt/TrueNas
```

**Every option there earns its place**, and the defaults are wrong for this
workload:

| Option | Why |
|---|---|
| `hard` | **The important one.** CIFS defaults to `soft`, which fails an I/O with `EAGAIN` when the server stalls. On a multi-hour 25 GB write that means MakeMKV takes the error, writes a short file, and **exits zero** — a truncated movie that looks complete. `hard` retries instead, turning a server hiccup into a visible pause. |
| `uid=`/`gid=` | Without them the share mounts root-owned and the app cannot write — it shows as "exists but not writable". |
| `nofail` | The box boots even if the NAS is down, instead of dropping to an emergency shell. |
| `_netdev` | Waits for the network. Without it the mount races startup and fails on reboot even though `mount -a` works by hand. |
| `x-systemd.automount` | Mounts on first access, so a share that drops comes back by itself. |

`hard` has one real cost: if the NAS disappears entirely, I/O blocks
indefinitely and those processes cannot be killed. Recover with
`sudo umount -f -l /mnt/TrueNas`. That trade is deliberate — a visible stall
beats a silently truncated file in your library.

### 2. Install the app

```bash
git clone git@github.com:YOURUSER/BluRayRipper.git && cd BluRayRipper
./scripts/setup-linux.sh --langs pol                  # rip box
./scripts/setup-linux.sh --langs pol --no-makemkv     # encode-only worker
```

This installs HandBrake, MKVToolNix, ffmpeg, Tesseract (+ language data),
dovi_tool, a JRE, and builds `seconv`; then installs BluRayRipper itself into
`.venv/` and links it onto `PATH`.

> Do **not** run `pip install -e .` by hand. Ubuntu 24.04 and other PEP 668
> distros mark the system Python externally managed and pip refuses, with or
> without `sudo`. The script uses a virtualenv, which is the supported route.

### 3. `config/secrets.env` — two lines

```bash
cp config/secrets.env.example config/secrets.env
```

Reduce it to exactly:

```
BLURAYRIPPER_DATABASE_URL=postgresql://bluray:PASSWORD@HOST:5432/blurayripper
BLURAYRIPPER_SECRETS_KEY=the-passphrase-from-part-1
```

**Nothing else belongs here.** Tool paths auto-detect; storage paths, cluster
options and credentials all come from Postgres. These two cannot: the database
URL is how the box *reaches* everything else, and the passphrase is what
decrypts it.

> `secrets.env` **overrides** the shared config. A leftover line silently wins
> over the cluster copy, so moving a value means deleting the local one.

---

## Part 3 — the rip box (`serve`)

```bash
blurayripper db-init          # create the schema
```

Enter the shared settings once, on the **Settings page** (global scope) after
starting `serve`. These reach every box:

| Group | Field | Example |
|---|---|---|
| Storage | Staging dir | `/mnt/TrueNas/.../BluRayRipper/staging` |
| Storage | Output dir | `/mnt/TrueNas/.../BluRayRipper/output` |
| Storage | Path map | `Z:\media = /mnt/TrueNas/media` |
| Cluster | Use workers | on |

**Path map** only matters if jobs were added on another OS; it rewrites stored
absolute paths. A path valid on the current box is never rewritten, so one rule
is safe everywhere.

Then publish the API credentials. Put them in `secrets.env` temporarily, push,
and delete the plaintext lines:

```bash
blurayripper secrets push && blurayripper secrets status
```

`status` lists names only — values are never printed. Other boxes pick them up
at their next start, given the same `BLURAYRIPPER_SECRETS_KEY`.

Install the service:

```bash
./scripts/install-service-linux.sh --role serve --requires-mount /mnt/TrueNas
```

---

## Part 4 — an encode worker

Nothing to configure: it inherits settings and credentials from Postgres.

```bash
./scripts/install-service-linux.sh --role worker --requires-mount /mnt/TrueNas
```

`--requires-mount` makes systemd hold the worker until the NAS is up. Without
it, a worker that wins the boot race against the mount claims jobs it cannot
read.

Two workers on one host need distinct ids:

```bash
./scripts/install-service-linux.sh --role worker --worker-name encoder-a
```

---

## Part 5 — verify

```bash
blurayripper check                 # rip box
blurayripper check --encode-only   # worker: MakeMKV is expected to be absent
```

Check, in order:

1. **Storage** shows the `/mnt/...` paths with **Writable: yes**. If it shows
   `./staging`, the shared settings did not arrive — see below.
2. **"Shared settings pulled from the cluster (N sections)"**.
3. **"TMDB key present"** on a box with no TMDB key in `secrets.env` — that
   proves the encrypted credentials decrypted.
4. **"All checks passed."**

Then confirm the worker registers, in the web UI's Workers panel or via the API.

---

## Troubleshooting

### Workers sit idle while jobs are queued

A worker claims a job, cannot find its ripped file, and hands it back. Since
v0.1.0+300 it says so instead of reporting `idle`:

```
NOT ENCODING: turned down 12 jobs in a row — their ripped files are not
reachable from this box. Missing: /mnt/TrueNas/.../job_46/rip/t000/x.mkv
```

Almost always the mount: wrong path, not mounted, or mounted root-only. Compare
`blurayripper check` on the worker against the rip box — the staging paths must
be identical strings.

### `check` says `./staging` instead of the NAS

The shared settings did not arrive. Either the box cannot reach Postgres, or the
values were never entered on the Settings page. Env vars do **not** seed the
shared config — a value only in `secrets.env` reaches no other box.

### Worker restart-loops with `PermissionError` on the mount point

The NAS is not mounted, and something tried to create a directory under it. Fixed
in v0.1.0+307 (storage is opened lazily), but the underlying cause is the mount.
`findmnt /mnt/TrueNas` then `sudo mount -a`.

### "Some checks failed" on a healthy worker

`makemkvcon` is missing because you built an encode-only box. Use
`blurayripper check --encode-only`; the setup script passes it automatically
when run with `--no-makemkv`.

### `pip install -e .` — "externally-managed-environment"

PEP 668. Use the virtualenv the setup script creates:

```bash
.venv/bin/python -m pip install -e ".[web,postgres,secrets]"
```

If `blurayripper` is not on `PATH`, find the install that the service uses and
link it:

```bash
ps -eo cmd | grep "[b]lurayripper"
sudo ln -sf /path/to/.venv/bin/blurayripper /usr/local/bin/blurayripper
```

### A rip failed with "Resource temporarily unavailable"

A `soft` CIFS mount returning `EAGAIN` mid-write. See the `hard` option in Part
2 — this silently truncates rips and is the single highest-value setting in this
document.

---

## Keeping boxes in step

Every deployed box pulls from `main`:

```bash
git pull && systemctl --user restart blurayripper-worker.service   # or -serve
```

Settings, presets, the box-set plan, and credentials all live in Postgres, so a
box that has been away picks up any changes at its next start. Only `main` and
`secrets.env` are per-box.
