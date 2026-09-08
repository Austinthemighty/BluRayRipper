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


# --- confirming must acknowledge the click -----------------------------------

def test_every_confirm_button_is_disabled_while_its_request_is_in_flight():
    """The reported bug: Confirm naming gave no feedback, the form stayed on
    screen, so it looked like nothing happened and people clicked again — and
    the second click landed on a job that had already left the naming gate.

    "Confirm titles" always did this correctly; the other two did not."""
    src = INDEX.read_text()
    for handler, guard in (
            ("doConfirm", "cbtn.disabled = true"),
            ("data-confirmmatch", "mbtn.disabled = true"),
            ("goBtn.onclick", "goBtn.disabled = true"),
    ):
        assert guard in src, f"{handler} does not disable while in flight"
    # ...and a re-entrant click is refused even before the disable takes effect
    assert "if (cbtn.disabled) return;" in src
    assert "if (mbtn.disabled) return;" in src


def test_a_failed_confirm_re_enables_its_button_so_it_can_be_retried():
    """Disabling forever would turn a transient failure into a dead form."""
    src = INDEX.read_text()
    assert "cbtn.disabled = false; cbtn.textContent = 'Confirm naming';" in src
    assert "mbtn.disabled = false;" in src


def test_confirming_removes_the_form_without_waiting_for_the_next_tick():
    """The form staying put is what invited the second click. The SSE frame can
    lag when the queue is large or the box is busy, so the committed transition
    is reflected immediately."""
    src = INDEX.read_text()
    confirm = src.split("async function doConfirm")[1].split("body.querySelector('[data-confirm]').onclick")[0]
    assert "renderedState[j.id] = null" in confirm
    assert "renderJobs(renderJobs._lastSnap)" in confirm


# --- the queue search box ----------------------------------------------------

def _named(**kw):
    base = {"id": 311, "state": "ripping", "display_name": "Happy Feet Two (2011)",
            "disc_name": "Happy Feet 2", "source": "disc:1"}
    base.update(kw)
    return json.dumps(base)


def test_search_finds_a_job_by_the_name_you_would_recognise():
    assert js(f"matchesQuery({_named()}, 'happy feet')") is True
    assert js(f"matchesQuery({_named()}, 'elemental')") is False


def test_search_matches_the_disc_label_not_just_the_tidy_name():
    """The label is what's printed on the disc in your hand ("Happy Feet 2"),
    which is rarely the library name ("Happy Feet Two (2011)"). Searching only
    display_name would miss the disc you are actually holding."""
    assert js(f"matchesQuery({_named()}, 'happy feet 2')") is True


def test_search_matches_the_source_path_for_file_jobs():
    """Most of this queue is file-sourced; the path is often the only thing that
    distinguishes two jobs."""
    j = _named(display_name=None, disc_name=None, source=r"Z:\\media\\Elemental (2023).mkv")
    assert js(f"matchesQuery({j}, 'elemental')") is True


def test_search_finds_a_job_by_its_id():
    """You read '311' off a card or a log line and want that job."""
    assert js(f"matchesQuery({_named()}, '311')") is True
    assert js(f"matchesQuery({_named()}, '#311')") is True
    assert js(f"matchesQuery({_named()}, '350')") is False


def test_every_word_must_match_so_extra_words_narrow():
    """ORing the terms would make each extra word return MORE jobs — the
    opposite of what typing more means."""
    assert js(f"matchesQuery({_named()}, 'happy 2011')") is True
    assert js(f"matchesQuery({_named()}, 'happy 1999')") is False


def test_an_empty_search_hides_nothing():
    """The box starts empty and must not filter the queue to nothing on load."""
    assert js(f"matchesQuery({_named()}, '')") is True
    assert js(f"matchesQuery({_named()}, '   ')") is True


def test_search_is_case_insensitive():
    assert js(f"matchesQuery({_named()}, 'HAPPY FEET')") is True


def test_a_job_with_no_name_or_disc_does_not_crash_the_search():
    """A job that failed before it was ever scanned has null display_name AND
    null disc_name. Joining nulls would throw and take the whole list render
    down with it."""
    j = json.dumps({"id": 7, "state": "failed", "display_name": None,
                    "disc_name": None, "source": None})
    assert js(f"matchesQuery({j}, 'anything')") is False
    assert js(f"matchesQuery({j}, '7')") is True


# --- the maintenance window's clock conversions ------------------------------

def test_a_deadline_is_sent_as_utc_not_as_whatever_the_browser_calls_it():
    """A datetime-local input carries no timezone. Sending it raw would mean a
    different instant on every box in the cluster."""
    iso = js("localToUtcIso('2026-08-30T02:00')")
    assert iso is not None and iso.endswith("Z")


def test_a_blank_deadline_is_not_turned_into_a_date():
    """An empty box must read as "nothing scheduled", not as the epoch — which
    would be a deadline permanently in the past and hold the cluster forever."""
    assert js("localToUtcIso('')") is None
    assert js("localToUtcIso('not a date')") is None


def test_a_stored_deadline_comes_back_into_the_form():
    """Reopening the dialog has to show what is actually scheduled; a blank box
    would invite you to overwrite a window you meant to keep."""
    assert js("utcIsoToLocalInput('') === ''") is True
    out = js("utcIsoToLocalInput(new Date(Date.UTC(2026,7,30,2,0)).toISOString())")
    assert len(out) == 16 and out[10] == "T"


def test_a_countdown_reads_in_hours_and_minutes():
    assert js("fmtLeft(3600*3 + 60*15)") == "3h 15m"
    assert js("fmtLeft(900)") == "15m"
    assert js("fmtLeft(0)") == "now"
    assert js("fmtLeft(-10)") == "now"
    assert js("fmtLeft(null)") == ""
