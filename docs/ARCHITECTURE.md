# How it fits together

A map for anyone reading the source for the first time. [docs/SETUP.md](SETUP.md)
covers deploying it; this is what the pieces are and why they're split up.

---

## The shape

One **rip box** owns the optical drives and the queue. Any number of **workers**
take the heavy encodes. They never talk to each other — everything goes through
Postgres and a shared filesystem:

```
  disc ──► rip box ──► shared staging ──► worker ──► shared output ──► Plex
             │                              │
             └────────── Postgres ──────────┘
                     queue · settings · credentials
```

A single machine is the same code with `cluster.use_workers` off: it rips and
encodes in one process, and Postgres is optional.

## A job's life

`pipeline.py` is the state machine, and the states are the stages:

```
SCANNING ─► SELECTING ─► AWAITING_MAPPING ─► READY ─► RIPPING
                                                        │
   DONE ◄── PLACING ◄── ENCODING ◄── OCR_SUBS ◄── EXTRACTING_SUBS
```

Two properties matter more than the order:

**Every stage checkpoints.** A job records what it has finished (`ripped_path`,
`placed`, extracted subtitle paths) so a restart resumes instead of redoing. This
is why a killed encode costs an encode and not a rip.

**The states are also a lock.** `RIPPING` means a drive is held; the serve box
won't let a second job touch that drive, and a worker won't claim a job whose rip
isn't finished.

## Who does what

| Area | Modules |
|---|---|
| State machine, stage order, checkpoints | `pipeline.py` |
| Web UI's job store, recovery, notifications | `web/manager.py` |
| HTTP + SSE (documented at `/docs`) | `web/app.py` |
| Encode worker: claim, encode, heartbeat | `worker.py` |
| Queue + settings + credentials in Postgres | `persistence.py`, `settings_store.py` |
| External tools, each wrapped one-per-file | `tools/` |
| Naming, library placement, subtitles, metadata | `services/` |

`tools/` is deliberately thin: one module per binary (`makemkv`, `handbrake`,
`mkvtools`, `ffmpeg`, `dovitool`, `subtitleedit`), each turning a CLI into an
async call with streamed progress. Everything that decides *what* to run lives
above it.

## Four things that will bite you

**Config precedence, lowest to highest:**

```
config/settings.toml  <  settings.local.json  <  per-worker Postgres overlay  <  secrets.env / env
```

Environment wins over shared config. That is deliberate — a box must be able to
override the cluster — but it means a stale line in `secrets.env` silently beats
the value you just saved in the web UI, on that box only.

**Workers resolve paths under their OWN `staging_dir`.** If it isn't the same
directory the rip box wrote to, a worker claims jobs, can't find the files, and
hands them straight back. `services/pathmap.py` exists for the other half of
this: a job added on Windows stores `Z:\...`, and a Linux worker rewrites it
through the configured path map.

**Claiming is `FOR UPDATE SKIP LOCKED`.** Two workers never get the same job, and
a worker that dies mid-encode has its job reclaimed once its heartbeat goes
stale (`cluster.stale_after_seconds`). Liveness is measured by the *database's*
clock, never by either box's — see `test_chaos_cluster.py`.

**"I couldn't tell" is not "it's gone."** Storage here is a network share that
does occasionally vanish. Code that decides a file is missing, a rip is bad or a
schedule says stop has to distinguish a real answer from a failed question — an
`OSError` reading the share is not evidence the file was deleted. Several tests
exist only to pin that distinction, because getting it wrong once destroyed 121
jobs' checkpoints.

## Storage layout

```
staging_dir/job_<id>/rip/t000/…     ripped titles, per job and title
staging_dir/job_<id>/sub/t000/…     extracted .sup and OCR'd .srt
output_dir/Movies/Title (Year)/…    finished library, Plex naming
output_dir/TV/Show (Year)/Season 01/…
```

Staging is cleaned when a job completes; the library is never touched again.
