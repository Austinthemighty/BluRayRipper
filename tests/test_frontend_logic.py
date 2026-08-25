"""Front-end logic, run against the REAL inline script in index.html.

The UI had no tests at all, and the bugs it shipped were the kind a user hits
within a minute: deleting a job scrolled you to the bottom of the page, turning
a page threw you to the top, and nine cards badged FAILED sat next to a "Failed
0" chip that filtered to nothing.

These evaluate the actual <script> under a DOM shim (tests/frontend/harness.js)
rather than a copy, so the file cannot drift away from what is asserted here.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INDEX = REPO / "src" / "blurayripper" / "web" / "static" / "index.html"
HARNESS = REPO / "tests" / "frontend" / "harness.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node is not installed on this machine")


def js(expr):
    """Evaluate an expression in the loaded UI script and return its value."""
    out = subprocess.run(
        ["node", str(HARNESS), str(INDEX), json.dumps({"expr": expr})],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout)


def _job(**kw):
    base = {"id": 1, "state": "ready", "lane": "encode", "stalled": False,
            "needs_review": False}
    base.update(kw)
    return json.dumps(base)


# --- the chip, the badge and the heading must agree --------------------------

def test_a_job_needing_review_is_not_counted_as_an_ordinary_failure():
    """It shipped with nine cards badged FAILED, a "Failed 0" chip, and a
    "Needs review 9" chip. Clicking Failed showed nothing while the FAILED cards
    were on screen."""
    assert js(f"catOf({_job(state='failed', needs_review=True)})") == "review"
    assert js(f"catOf({_job(state='failed', needs_review=False)})") == "fail"


def test_a_review_job_sorts_above_everything_including_other_failures():
    """It is the group that needs a physical action — putting the disc back — so
    it goes where you look first."""
    review = js(f"jobRank({_job(state='failed', needs_review=True)})")
    failed = js(f"jobRank({_job(state='failed')})")
    done = js(f"jobRank({_job(state='done')})")
    assert review < failed < done


def test_every_rank_has_a_heading_and_they_are_in_order():
    """A rank with no label renders an unlabelled group; ranks that collide put
    two different kinds of job under one heading. Both shipped once."""
    labels = js("GROUP_LABELS")
    ranks = js("""[
      {state:'failed', needs_review:true}, {state:'failed'},
      {state:'awaiting_mapping'}, {state:'ripping', lane:'rip'},
      {state:'ready', lane:'rip'}, {state:'encoding', lane:'encode'},
      {state:'ready', lane:'encode'}, {state:'done'}
    ].map(jobRank)""")
    assert ranks == sorted(ranks), ranks
    assert len(set(ranks)) == len(ranks), "two job kinds share a heading"
    assert max(ranks) < len(labels), "a rank has no heading"
    assert labels[0] == "Needs review" and labels[-1] == "Completed"


# --- the two scroll bugs -----------------------------------------------------

def test_paging_scrolls_the_list_into_view_rather_than_jumping_the_document():
    """Next jumped the window to the top, which scrolls the queue header and the
    pager itself off screen — the control you just used disappears. Prev did not
    scroll at all, leaving you mid-list on a different page."""
    src = INDEX.read_text()
    assert "window.scrollTo({top:0" not in src, "still jumps to the document top"
    # Both controls go through the same helper.
    assert src.count("toPageTop()") == 2, "prev and next must behave the same"


def test_the_page_turn_scroll_is_instant_because_a_re_render_cancels_a_smooth_one():
    """The queue re-renders on every server tick and each re-render cancels an
    in-flight smooth scroll, so the animation never arrived: you were left where
    the now-shorter page clamped you (a 50-card page becomes a 9-card page)."""
    src = INDEX.read_text()
    helper = src.split("const toPageTop")[1].split("};")[0]
    # Strip comments first: the comment explains WHY it is not smooth, so a
    # naive substring check finds the word it is warning about.
    code = "\n".join(l for l in helper.splitlines()
                     if not l.strip().startswith("//"))
    assert "behavior: 'auto'" in code, code
    assert "smooth" not in code, code


def test_a_job_that_merely_moved_onto_this_page_is_not_treated_as_new():
    """The "new job" set was the ids on the CURRENT PAGE, so deleting a job —
    which pulls the next one up from page 2 — made that job look new and scrolled
    you to it, i.e. to the bottom of the list. It must be every id in the
    snapshot."""
    src = INDEX.read_text()
    assert "renderJobs._known = allIds;" in src
    assert "const allIds = new Set(snap.jobs.map(j => j.id));" in src
    assert "renderJobs._known = new Set(nowIds);" not in src


# --- things that were already right and must stay that way -------------------

def test_a_stalled_job_still_reads_as_stalled_whatever_its_state():
    """Stalled is the more urgent thing to say: the state says "encoding" and
    nothing is encoding it."""
    assert js(f"catOf({_job(state='encoding', stalled=True)})") == "enc"
    src = INDEX.read_text()
    assert "j.stalled ? 'stalled'" in src


def test_a_ready_job_is_queued_on_whichever_side_it_is_waiting():
    assert js(f"catOf({_job(state='ready', lane='encode')})") == "queued"
    assert js(f"catOf({_job(state='ready', lane='rip')})") == "queued"
