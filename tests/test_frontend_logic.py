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



def test_the_pinned_top_bar_does_not_cover_what_scrolls_to_the_top():
    """With the top bar pinned, a page turn or a jump to a job puts its target
    at the top of the window, which is under the bar. The root's scroll-padding
    has to clear the bar's live height, and the sidebar has to stick below it."""
    src = INDEX.read_text()
    assert "header { position: sticky; top: 0;" in src
    assert "scroll-padding-top: calc(var(--head-h, 0px) + 8px)" in src
    assert ".side { position: sticky; top: calc(var(--head-h, 0px) + 16px)" in src
    assert "trackHeaderHeight();" in src
    helper = src.split("const toPageTop")[1].split("};")[0]
    assert "scrollIntoView({block: 'start'" in helper, "a raw scrollTo ignores scroll-padding"


def test_on_a_phone_the_pinned_bar_is_slim_with_the_actions_behind_a_menu():
    """The full bar wraps to about 260px on a phone, a third of the screen, so
    pinned it is only the title, search and a menu button. Everything else sits
    in the menu, which must stay a plain part of the bar on a wide screen."""
    src = INDEX.read_text()
    head = src.split("<header>")[1].split("</header>")[0]
    bar, menu = head.split('<div class="head-actions" id="headactions">')
    assert 'id="jobsearch"' in bar and 'id="headmenu"' in bar
    for piece in ('id="ver"', 'id="uptime"', 'id="redoall"', 'id="opensettings"',
                  'id="autoconfirm"', "openSeasons()"):
        assert piece in menu, piece
    assert ".head-actions { display: contents; }" in src
    assert ".head-actions.open {" in src
    assert "wireHeadMenu();" in src


def test_the_wide_bar_keeps_only_search_and_the_tools_out_of_the_settings_menu():
    """Nine equal buttons made the bar a wall. Settings, the HandBrake preset,
    auto-confirm and the two site-wide maintenance actions live in one Settings
    menu; the box sets and seasons tools stay in the bar."""
    src = INDEX.read_text()
    head = src.split("<header>")[1].split("</header>")[0]
    outside, rest = head.split('<div class="menu" id="settingspanel">')
    settings = rest.split("</div>\n    </div>")[0]
    for piece in ('id="opensettings"', "openHandbrake()", 'id="autoconfirm"',
                  'id="redoall"', 'id="refreshart"'):
        assert piece in settings, piece
    for piece in ("openBoxsets()", "openSeasons()", 'id="settingsmenu"'):
        assert piece in outside, piece
    # The maintenance buttons report their result in their own label, so
    # picking one must not close the menu over it.
    for bulk in ('id="redoall"', 'id="refreshart"'):
        line = next(l for l in settings.splitlines() if bulk in l)
        assert "data-stay" in line, line


def test_a_long_file_path_in_a_card_wraps_instead_of_widening_the_page():
    """A failed rip's error and a finished job's output line carry paths like
    /mnt/TrueNas/achopra/MakeMKV/... with no spaces to wrap at, which pushed a
    phone's page out to ~480px so it dragged sideways. Cards must break a long
    word; break-word rather than anywhere, which would also squeeze the title
    tables' columns."""
    src = INDEX.read_text()
    rule = src.split("  .job {", 1)[1].split("}", 1)[0]
    assert "overflow-wrap: break-word" in rule, rule


def test_a_notification_link_to_a_job_opens_the_page_at_that_job():
    """ntfy opens /#job-N when a job's alert is tapped. The page brings that
    job into view once the queue has loaded, then drops the hash so a reload
    doesn't jump back to it."""
    out = js("window.location.hash = '#job-431';"
             "let replaced = null;"
             "window.history = {replaceState(a, b, url) { replaced = url; }};"
             "window.location.pathname = '/'; window.location.search = '';"
             "renderJobs._lastSnap = {jobs: []};"
             "let went = null; goToJob = (id) => { went = id; };"
             "openLinkedJob();"
             "const first = [went, replaced];"
             "went = null; window.location.hash = '#queue'; openLinkedJob();"
             "[first, went]")
    assert out == [[431, "/"], None]
    src = INDEX.read_text()
    assert "renderJobs(snap); openLinkedJob();" in src
    assert "addEventListener('hashchange', openLinkedJob)" in src

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
    assert js(f"stallBadge({_job(state='encoding', stalled=True)}).text") == "stalled"


def test_a_stall_that_kept_its_rip_is_not_dressed_up_as_a_failure():
    """Two jobs stalled the same evening: one whose drive died part-way through
    the rip, and one that ripped its disc, verified it, and then stopped dead
    afterwards. Both cards showed the same red "stalled", so the second sent its
    operator looking for a disc to re-insert over a rip that had already
    succeeded and was sitting on disk. Amber, and say which it is."""
    saved = _job(state="ripping", lane="rip", stalled=True, stall_kind="rip_saved")
    lost = _job(state="ripping", lane="rip", stalled=True, stall_kind="rip_unfinished")
    assert js(f"stallBadge({saved}).text") == "stalled · rip saved"
    assert js(f"stallBadge({saved}).cls") == "await"    # amber: nothing was lost
    assert js(f"stallBadge({lost}).text") == "stalled"
    assert js(f"stallBadge({lost}).cls") == "failed"
    # needs_review keeps its own wording; a stall doesn't swallow it.
    assert js(f"stallBadge({_job(state='failed', needs_review=True)}).text") \
        == "needs review"


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


# --- a disc with two cuts of one film ----------------------------------------

def test_runtimes_are_shown_so_you_can_tell_the_cuts_apart():
    """The runtime is the only thing distinguishing the rows: a theatrical and an
    unrated cut carry the same title and differ by minutes."""
    assert js("fmtRuntime(7512)") == "2h05m"
    assert js("fmtRuntime(7888)") == "2h11m"
    assert js("fmtRuntime(216)") == "4m"
    assert js("fmtRuntime(0)") == "0m"


def test_a_runtime_never_renders_sixty_minutes():
    """7199s rounds to 120 minutes. Rounding the remainder rather than the whole
    gives "1h60m" — rare, but it appears exactly where someone is trying to tell
    two cuts apart by their length."""
    assert js("fmtRuntime(7199)") == "2h00m"
    assert js("fmtRuntime(3599)") == "1h00m"


def test_each_cut_gets_its_own_edition_control():
    """One Edition box for a two-cut disc meant both titles took the same value,
    so both computed the same filename."""
    j = json.dumps({"id": 363, "selected": [0, 1],
                    "titles": [{"id": 0, "seconds": 7512}, {"id": 1, "seconds": 7711}]})
    html = js(f"editionPerTitleHtml({j})")
    assert html.count("data-edrow") == 2
    assert 'data-edrow="0"' in html and 'data-edrow="1"' in html
    assert "2h05m" in html and "2h09m" in html


def test_a_title_the_disc_did_not_report_still_renders_a_row():
    """A selected id with no matching entry in titles must not throw and take the
    whole naming form down with it."""
    j = json.dumps({"id": 1, "selected": [0, 9], "titles": [{"id": 0, "seconds": 6000}]})
    html = js(f"editionPerTitleHtml({j})")
    assert html.count("data-edrow") == 2


# --- telling a theatrical cut from an unrated one ----------------------------

def _titles(*pairs):
    return json.dumps([{"id": i, "seconds": s} for i, s in pairs])


def test_tmdbs_runtime_identifies_the_theatrical_cut():
    """Job #363: the disc offered 125m and 129m, and TMDB lists 125m for Fifty
    Shades of Grey. The matching title is the theatrical cut; the longer one is
    whatever the disc calls its alternate."""
    out = js(f"suggestEditions({_titles((0,7512),(1,7711))}, [0,1], 125)")
    assert out["0"]["edition"] == "Theatrical Cut"
    assert out["1"]["edition"] == "Extended Cut"
    assert "matches" in out["0"]["note"] and "+4m" in out["1"]["note"]


def test_it_works_when_the_cuts_are_far_apart():
    """Job #361: 118m and 131m against TMDB's 118m."""
    out = js(f"suggestEditions({_titles((0,7068),(3,7888))}, [0,3], 118)")
    assert out["0"]["edition"] == "Theatrical Cut"
    assert out["3"]["edition"] == "Extended Cut"
    assert "+13m" in out["3"]["note"]


def test_the_theatrical_cut_is_found_whatever_order_the_titles_are_in():
    """Nothing may assume the shorter cut comes first — on #361 it did, on other
    discs it won't."""
    out = js(f"suggestEditions({_titles((0,7888),(1,7068))}, [0,1], 118)")
    assert out["1"]["edition"] == "Theatrical Cut"
    assert out["0"]["edition"] == "Extended Cut"


def test_nothing_is_suggested_when_no_cut_is_close_to_tmdb():
    """If TMDB lists an edition this disc doesn't carry, every label would be a
    guess. A confident wrong label is worse than none — the operator still gets
    the deltas and decides."""
    out = js(f"suggestEditions({_titles((0,7068),(1,7888))}, [0,1], 95)")
    assert out["0"]["edition"] == "" and out["1"]["edition"] == ""
    assert "no close match" in out["0"]["note"]


def test_no_runtime_from_tmdb_means_no_suggestion_and_no_crash():
    """No API key, TMDB down, or a film it has no runtime for. The form must
    still work — this is a hint, not a dependency."""
    for arg in ("null", "0", "undefined"):
        out = js(f"suggestEditions({_titles((0,7068),(1,7888))}, [0,1], {arg})")
        assert out["0"]["edition"] == "" and out["0"]["note"] == ""


def test_a_suggested_edition_is_preselected_in_the_dropdown():
    """The suggestion is only useful if it arrives chosen; making the operator
    re-pick what we just worked out would defeat it."""
    html = js("editionSelectHtml('Theatrical Cut')")
    assert "<option selected>Theatrical Cut</option>" in html
    assert 'value="" selected' not in html


def test_with_no_suggestion_the_dropdown_starts_on_none():
    html = js("editionSelectHtml('')")
    assert 'value="" selected' in html


# --- which drive can read a UHD disc ----------------------------------------

def test_a_drive_that_cannot_read_uhd_says_so_before_a_disc_is_queued():
    """Two physically identical ASUS drives on this box behaved completely
    differently — one had patched LibreDrive firmware and read UHD, the other
    could not read it at all and its SCSI retries blocked drive detection for
    both. Nothing visible distinguished them; the firmware version pointed the
    wrong way, the HIGHER number being the capable one."""
    cap = json.dumps({"uhd_ok": True, "disc_bytes": 65e9})
    nocap = json.dumps({"uhd_ok": False, "disc_bytes": 48e9})
    mismatch = json.dumps({"uhd_ok": False, "disc_bytes": 65e9})
    unknown = json.dumps({"uhd_ok": None, "disc_bytes": 65e9})

    assert "UHD" in js(f"driveCapsHtml({cap})")
    assert "standard discs only" in js(f"driveCapsHtml({nocap})")
    # A standard-size disc in the limited drive is fine, so no warning.
    assert "cannot read it" not in js(f"driveCapsHtml({nocap})")

    warned = js(f"driveCapsHtml({mismatch})")
    assert "cannot read it" in warned and "65 GB" in warned

    # Never opened a disc in this drive -> nothing is known, so claim nothing.
    assert js(f"driveCapsHtml({unknown})") == ""


def test_a_drive_holding_an_unreadable_disc_does_not_read_as_empty():
    """"no disc / mounting…" covered both an empty tray and a disc the kernel
    can see but nobody has named yet — which is the state a disc sits in while
    another rip keeps makemkvcon busy, and the reason one had to be added by
    hand while the panel showed an empty drive."""
    named = json.dumps({"disc_name": "LANDMAN_SEASON2", "disc_bytes": 48627580928})
    unnamed = json.dumps({"disc_name": None, "disc_bytes": 48627580928})
    empty = json.dumps({"disc_name": None, "disc_bytes": None})

    assert "LANDMAN_SEASON2" in js(f"discLabelHtml({named})")
    assert "disc present" in js(f"discLabelHtml({unnamed})")
    assert "no disc" in js(f"discLabelHtml({empty})")
    assert "no disc" not in js(f"discLabelHtml({unnamed})")

    # The "(via OS)" note still rides along with an OS-recovered name.
    via_os = json.dumps({"disc_name": "LANDMAN", "os_detected": True, "disc_bytes": 1})
    assert "via OS" in js(f"discLabelHtml({via_os})")


def test_the_boxset_editor_knows_every_column_the_plan_does():
    """The table writes the saved CSV from its own column list, so a column it
    doesn't know is deleted from the file the moment anyone switches from the
    CSV view back to the table — silently, and after they have typed it in."""
    src = INDEX.read_text()
    cols = src.split("const BOXSET_COLS = [")[1].split("];")[0]
    for key in ("disc_label", "show", "year", "season", "disc",
                "collection", "first_episode", "episodes"):
        assert f"key:'{key}'" in cols, f"the editor would drop “{key}” on save"


# --- a two-parter mastered as one title -----------------------------------------

def _lengths(*seconds):
    return json.dumps([{"id": i, "seconds": s} for i, s in enumerate(seconds)])


def test_fill_in_order_gives_a_double_length_title_two_episodes():
    """Job 399, HIMYM S8 D2: title 2 is "The Final Page" parts 1 and 2. Filling
    one episode per title made it E11 and pushed every title after it one
    episode short — title 3, really E13, came out as E12."""
    filled = js(f"fillInOrder({_lengths(1296, 1298, 2595, 1221, 1297, 1297, 1296)}, 9)")
    assert filled["assign"] == {"0": 9, "1": 10, "2": 11, "3": 13, "4": 14, "5": 15, "6": 16}
    assert filled["ends"] == {"2": 12}


def test_the_form_and_the_server_spot_the_same_two_parters():
    """The box-set plan applies the same rule server-side; if the two drifted, a
    disc could name itself one way and fill the form another."""
    from blurayripper.models import Title
    from blurayripper.tools.discinfo import double_length_titles
    for lengths in [(1296, 1298, 2595, 1221, 1297, 1297, 1296),
                    (1297, 1262, 1296, 1297, 1296, 1296, 2631, 2590),
                    (2580, 2610, 2595, 2640), (1200, 2600, 2620, 2580),
                    (1296, 2595, 1290, 2610), (300, 1296, 2595),
                    # Either side of both thresholds (1.7x and 2.4x of 1300s),
                    # so a rule that drifts on either side shows up here.
                    (1300, 1300, 2150, 2230, 3100, 3185)]:
        server = double_length_titles([Title(title_id=i, duration_seconds=s)
                                       for i, s in enumerate(lengths)])
        assert set(js(f"doubleLengthIds({_lengths(*lengths)})")) == server, lengths


def test_a_worker_that_cannot_update_is_marked_and_told_how_to_fix_it():
    stuck = js('workerCodeBadge({code: "off_main"})')
    assert "Can't update itself" in stuck and "git reset --hard origin/main" in stuck
    assert "3 commits behind" in js('workerCodeBadge({code: "behind", code_commits: 3})')
    assert js('workerCodeBadge({code: "current", code_commits: 0})') == ""
    assert js('workerCodeBadge({})') == ""            # an older server sends no verdict


# --- the drive panel says whose disc it is, and takes you there ---------------

def test_the_drive_row_names_the_job_and_whether_it_is_reading():
    ripping = js('driveJobHtml({job: {id: 398, name: "How I Met Your Mother S08 D01", reading: true}})')
    assert "Ripping: #398 How I Met Your Mother S08 D01" in ripping and 'data-gojob="398"' in ripping
    assert "Waiting: #399" in js('driveJobHtml({job: {id: 399, name: "x", reading: false}})')
    assert js('driveJobHtml({job: null})') == ""


def test_going_to_a_job_finds_the_page_it_is_on():
    """With 150+ jobs paged 50 at a time, the job a drive points at is usually
    not on the page you are looking at."""
    jobs = json.dumps([{"id": i, "state": "done", "lane": None} for i in range(1, 121)])
    assert js(f"jobPageOf({jobs}, 1, '50')") == 0
    assert js(f"jobPageOf({jobs}, 51, '50')") == 1
    assert js(f"jobPageOf({jobs}, 120, '50')") == 2
    assert js(f"jobPageOf({jobs}, 120, 'all')") == 0
    assert js(f"jobPageOf({jobs}, 999, '50')") == -1


# --- a job's titles on its card ------------------------------------------------------

def _titled_job(state):
    return json.dumps({"id": 388, "state": state, "titles": [
        {"id": 0, "duration": "0:50:28", "size_gb": 11.8, "playlist": "00800.mpls",
         "picked": True, "status": "ripped", "named": "S02E08"},
        {"id": 1, "duration": "0:56:34", "size_gb": 13.2, "playlist": "00801.mpls",
         "picked": True, "status": "ripping", "named": "S02E09"},
        {"id": 3, "duration": "0:28:18", "size_gb": 4.1, "playlist": "01085.m2ts",
         "picked": False, "status": "not_picked", "named": None}]})


def test_the_card_lists_the_titles_and_how_many_are_ripped():
    sec = js(f"titlesSection({_titled_job('ripping')})")
    assert sec["summary"] == "Titles — 2 of 3 picked · 1 ripped · 1 ripping"
    assert "S02E08" in sec["body"] and "ripped" in sec["body"]
    assert "ripping…" in sec["body"] and "not picked" in sec["body"]
    assert "00800.mpls" in sec["body"] and "11.8 GB" in sec["body"]


def test_the_title_list_stays_out_of_the_way_while_you_pick_titles():
    assert js(f"titlesSection({_titled_job('awaiting_titles')})") is None
    assert js('titlesSection({id: 1, state: "ready", titles: []})') is None


def test_titles_already_encoded_or_in_the_library_count_as_ripped():
    job = json.dumps({"id": 1, "state": "encoding", "titles": [
        {"id": 0, "picked": True, "status": "placed"},
        {"id": 1, "picked": True, "status": "encoded"},
        {"id": 2, "picked": True, "status": "ripped"}]})
    sec = js(f"titlesSection({job})")
    assert sec["summary"] == "Titles — 3 of 3 picked · 3 ripped"
    assert "in library" in sec["body"]


# --- the season on the form against the season on the label -------------------------

def test_a_season_that_disagrees_with_the_disc_label_is_called_out():
    """Job 397's label said S9; the form said 8, and nothing questioned it, so
    every title was compared with the wrong season's episodes."""
    said = js("seasonMismatch(9, '8')")
    assert "season 9" in said and "season 8" in said
    assert js("seasonMismatch(9, '9')") == ""
    assert js("seasonMismatch(null, '8')") == ""          # the label states none
    assert js("seasonMismatch(9, '')") == ""              # nothing chosen yet


def test_auto_identify_searches_every_episode_a_two_parter_adds():
    """Job 399: seven titles holding E09–E16. Counting titles searched E09–E15
    and left the last episode out."""
    titles = _lengths(1296, 1298, 2595, 1221, 1297, 1297, 1296)
    assert js(f"identifyEpisodes({titles}, 9)") == list(range(9, 17))
    assert js(f"identifyEpisodes({_lengths(1296, 1298, 1300)}, 1)") == [1, 2, 3]
    # The two-parter last on the disc, as a season finale often is.
    assert js(f"identifyEpisodes({_lengths(1296, 1298, 2595)}, 22)") == [22, 23, 24, 25]


# --- skipping a title that is not an episode --------------------------------------------

def test_skipping_the_last_title_leaves_the_episodes_alone():
    """Job 423: titles 0-5 were E17-E22 and the bonus came last."""
    r = js("renumberAfterSkip([0,1,2,3,4,5,6], {0:17,1:18,2:19,3:20,4:21,5:22,6:23}, {}, 6)")
    assert r["assign"] == {"0": 17, "1": 18, "2": 19, "3": 20, "4": 21, "5": 22}


def test_skipping_a_title_in_the_middle_moves_the_rest_down_one():
    r = js("renumberAfterSkip([0,1,2,3], {0:1,1:2,2:3,3:4}, {}, 1)")
    assert r["assign"] == {"0": 1, "2": 2, "3": 3}


def test_skipping_a_two_parter_moves_the_rest_down_two():
    r = js("renumberAfterSkip([0,1,2,3], {0:9,1:10,2:12,3:13}, {1:11, 3:14}, 1)")
    assert r["assign"] == {"0": 9, "2": 10, "3": 11}
    assert r["ends"] == {"3": 12}                      # a later two-parter keeps its pair


# --- the season check view and the naming-time warning ---------------------------------

def test_episode_runs_read_as_ranges():
    assert js("rangesText([16, 9, 10, 11, 12])") == "E09–E12, E16"


def test_a_season_report_names_the_gap_the_disc_and_the_clash():
    report = json.dumps({"show": "How I Met Your Mother", "year": 2005, "tmdb_id": 1100, "seasons": [
        {"season": 1, "status": "incomplete", "total": 22,
         "counts": {"library": 0, "queued": 14, "failed": 0, "missing": 8},
         "missing": [{"first": 9, "last": 16, "discs": ["S01 D02"]}], "failed": [],
         "conflicts": [], "remakes": [], "unexpected": []},
        {"season": 3, "status": "in_progress", "total": 20,
         "counts": {"library": 0, "queued": 20, "failed": 0, "missing": 0},
         "missing": [], "failed": [], "conflicts": [{"episode": 1, "jobs": [417, 419]}],
         "remakes": [], "unexpected": []}]})
    html = js(f"seasonReportHtml({report})")
    assert "Missing E09–E16 (box-set plan: S01 D02)" in html
    assert "❌ episodes missing" in html and "0 of 22 in the library · 14 queued" in html
    assert "E01 is named by jobs" in html and 'data-seasonjob="419"' in html


def test_naming_an_episode_another_job_has_says_so_first():
    claims = json.dumps({"1": [{"job": 417, "state": "ready"}],
                         "2": [{"job": 236, "state": "done"}],
                         "3": [{"job": 9, "state": "failed"}]})
    assignments = json.dumps([{"episode": 1}, {"episode": 2}, {"episode": 3}])
    warn = js(f"claimWarnings({assignments}, {claims}, 3)")
    assert warn == [
        "S03E01 is already named in job #417 (ready) — whichever is placed second overwrites the other.",
        "S03E02 is already in the library from job #236 — this would replace it.",
    ]                                                   # a failed job's claim doesn't count
