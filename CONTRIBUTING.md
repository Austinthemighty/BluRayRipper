# Contributing

## Running the tests

```bash
python -m venv .venv && .venv/bin/pip install -e ".[web,postgres,secrets]" pytest pytest-asyncio pytest-timeout
.venv/bin/python -m pytest -q
```

Around 2,000 tests, a few seconds to run, no network and no database — the
Postgres paths are exercised through a fake connection that asserts on the SQL.
A handful of front-end tests run the dashboard's real `<script>` under a small
DOM shim (`tests/frontend/harness.js`), so they need `node` on PATH and skip
themselves when it's missing.

`pyproject.toml` sets `pythonpath = ["src"]` and `asyncio_mode = "auto"`, so
async tests need no decorator and there's nothing to install first.

## What a test is for here

The convention in this repo is that **a test names the failure it prevents**, in
the words of what went wrong, not the function it calls:

```python
def test_a_worker_refuses_new_work_inside_the_window():
    """The hold has to sit BEFORE claim_next. A job claimed inside the window is
    precisely the job that runs past the deadline."""
```

That docstring is the point. Most of these tests exist because something broke in
a way that was expensive or slow to diagnose, and the docstring is what stops the
next person "simplifying" the guard away.

Two habits worth keeping:

**Assert on the outcome, not the wording.** A test that pins an error message
breaks when the message improves, which trains people to update tests without
reading them.

**Check the test can fail.** Before trusting a new test, break the line it covers
and confirm it goes red. A surprising number of plausible-looking tests pass
against deliberately broken source — usually because they assert on a mock, or on
a string that appears in a nearby comment.

## Style

- Comments explain **why**, not what. The code already says what it does; the
  comment should say what it cost to learn.
- Error text is written for whoever hits it at 2am: what happened, what it
  means, and what to do. Several of them name the specific failure that produced
  them.
- No new dependency without a reason that survives "could this be thirty lines?"

## Layout

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the module map and the
invariants that aren't obvious from the code, and [docs/SETUP.md](docs/SETUP.md)
for deploying a cluster.
