"""Unit coverage for four small modules: the git-derived version string, the
persisted friendly drive-name map, the finished-job subtitle audit, and the
OpenSubtitles manual-upload sheet."""

import json

import pytest

from blurayripper import version
from blurayripper.models import (DiscInfo, EpisodeAssignment, MediaKind, Stream,
                                 StreamType, Title)
from blurayripper.persistence import JobRecord, assignment_to_dict, disc_to_dict
from blurayripper.services import os_upload
from blurayripper.services.drive_names import DriveNames
from blurayripper.subtitle_audit import audit_done_jobs

# --- version ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_version_caches():
    """get_version()/_git_exe() are lru_cached; never leak a stubbed value."""
    version.get_version.cache_clear()
    version._git_exe.cache_clear()
    yield
    version.get_version.cache_clear()
    version._git_exe.cache_clear()


def _stub_git(monkeypatch, count=None, short=None, status=None):
    """Replace version._git with a canned responder. Returns the call log.

    None is what the real _git returns for a failed or empty command, so an
    unset argument here means "git couldn't answer that"."""
    answers = {
        ("rev-list", "--count", "HEAD"): count,
        ("rev-parse", "--short", "HEAD"): short,
        ("status", "--porcelain"): status,
    }
    calls = []

    def fake(*args):
        calls.append(args)
        return answers.get(args)

    monkeypatch.setattr(version, "_git", fake)
    return calls


def _fake_repo(root, monkeypatch, *, head, refs=None, packed=None):
    git = root / ".git"
    git.mkdir(parents=True)
    (git / "HEAD").write_text(head, encoding="utf-8")
    for name, sha in (refs or {}).items():
        ref_path = git / name
        ref_path.parent.mkdir(parents=True, exist_ok=True)
        ref_path.write_text(sha + "\n", encoding="utf-8")
    if packed is not None:
        (git / "packed-refs").write_text(packed, encoding="utf-8")
    monkeypatch.setattr(version, "_REPO", root)


def test_the_printed_version_can_be_parsed_back_into_a_build_number(monkeypatch):
    """The string exists so one box can compare itself to another. If the format
    and commit_count() ever drift apart, worker auto-update silently stops."""
    _stub_git(monkeypatch, count="242", short="bc2d392")
    v = version.get_version()
    assert v == f"{version._BASE}+242.bc2d392"
    assert version.commit_count(v) == 242


def test_a_dirty_checkout_is_marked_but_still_reports_its_build(monkeypatch):
    """A box with local edits is running something nobody else has; the label is
    the only warning. Marking it must not cost it its comparable build number."""
    _stub_git(monkeypatch, count="242", short="bc2d392",
              status=" M src/blurayripper/version.py")
    v = version.get_version()
    assert v == f"{version._BASE}+242.bc2d392-dirty"
    assert version.commit_count(v) == 242


def test_the_version_is_resolved_once_per_process(monkeypatch):
    """get_version() is hit by every status poll and page render; without the
    cache each one forks three git processes on the rip box."""
    calls = _stub_git(monkeypatch, count="7", short="abc1234")
    first = version.get_version()
    spawned = len(calls)
    second = version.get_version()
    assert spawned > 0                 # it really did shell out the first time
    assert len(calls) == spawned       # …and not again
    assert first == second


def test_builds_are_ordered_by_number_not_by_string(monkeypatch):
    """The worker decides whether to pull by comparing builds. Ordering the
    version strings would rank build 99 above build 100 and strand workers on
    stale code forever."""
    older, newer = "0.1.0+99.aaaaaaa", "0.1.0+100.bbbbbbb"
    assert older > newer  # the trap: lexically, "99" beats "100"
    assert version.commit_count(older) == 99
    assert version.commit_count(older) < version.commit_count(newer)


def test_commit_count_never_guesses_from_a_version_it_cannot_parse():
    """A caller that gets a number out of a git-less version would compare it
    against a real build count and either downgrade or spin on update."""
    for unparseable in (None, "", "0.1.0", "0.1.0+gabc1234", "unknown", "v1.2.3"):
        assert version.commit_count(unparseable) is None


def test_a_git_less_build_still_names_its_commit_without_claiming_a_number(monkeypatch):
    """Installed from a wheel / no git binary: the SHA still identifies what the
    box is running, but it must be shaped so the updater refuses to compare."""
    _stub_git(monkeypatch)  # every git invocation fails
    monkeypatch.setattr(version, "_sha_from_git_dir", lambda: "abc1234")
    v = version.get_version()
    assert v == f"{version._BASE}+gabc1234"
    assert version.commit_count(v) is None


def test_the_bare_base_version_is_the_last_resort(monkeypatch):
    """With neither git nor a .git dir there is nothing to append — appending
    anyway ("+gNone") would be reported to the cluster as a real build id."""
    _stub_git(monkeypatch)
    monkeypatch.setattr(version, "_sha_from_git_dir", lambda: None)
    v = version.get_version()
    assert v == version._BASE
    assert "None" not in v


def test_a_detached_head_still_identifies_the_build(tmp_path, monkeypatch):
    """Deploys that check out a tag land detached; the SHA sits in HEAD itself
    rather than behind a ref, and reading it wrong yields the literal "ref: re"."""
    _fake_repo(tmp_path, monkeypatch,
               head="4b07ff5abcdef1234567890abcdef1234567890a\n")
    assert version._sha_from_git_dir() == "4b07ff5"


def test_a_branch_checkout_is_followed_to_the_commit_it_points_at(tmp_path, monkeypatch):
    """Normal case on every deployed box: HEAD names a branch, and the version
    has to report the commit rather than the ref text."""
    _fake_repo(tmp_path, monkeypatch, head="ref: refs/heads/main\n",
               refs={"refs/heads/main": "9e1d0c7fedcba9876543210fedcba98765432100"})
    assert version._sha_from_git_dir() == "9e1d0c7"


def test_packed_refs_are_searched_for_the_right_branch(tmp_path, monkeypatch):
    """A fresh clone keeps its branches packed with no loose ref file. Without
    this fallback every newly cloned worker reports an unidentifiable version —
    and a sloppy search would hand back whichever branch was listed first."""
    _fake_repo(tmp_path, monkeypatch, head="ref: refs/heads/main\n", packed=(
        "# pack-refs with: peeled fully-peeled sorted \n"
        "aaaaaaa1111111111111111111111111111111111 refs/heads/other\n"
        "bbbbbbb2222222222222222222222222222222222 refs/heads/main\n"
    ))
    assert version._sha_from_git_dir() == "bbbbbbb"


def test_unreadable_git_metadata_yields_no_sha_rather_than_junk(tmp_path, monkeypatch):
    """Running from an unpacked zip, or from a repo whose ref is dangling: the
    fallback must decline instead of raising or inventing a SHA."""
    monkeypatch.setattr(version, "_REPO", tmp_path / "no-repo-here")
    assert version._sha_from_git_dir() is None

    _fake_repo(tmp_path / "dangling", monkeypatch, head="ref: refs/heads/main\n")
    assert version._sha_from_git_dir() is None  # no loose ref, no packed-refs


# --- drive names --------------------------------------------------------


def test_a_drive_label_typed_in_the_ui_survives_a_restart(tmp_path):
    """The map is the only home for a drive's human label, and it lives under a
    dot-directory that may not exist yet — so set() has to create it and write
    through, or every relabel vanishes the next time serve starts."""
    path = tmp_path / "output" / ".blurayripper" / "drive_names.json"
    DriveNames(path).set("F:", "Top drive")

    assert DriveNames(path).get("F:") == "Top drive"
    assert json.loads(path.read_text(encoding="utf-8")) == {"F:": "Top drive"}


def test_naming_a_second_drive_does_not_erase_the_first(tmp_path):
    """Each set() rewrites the whole file, so it must start from what is already
    on disk — otherwise labelling G: silently unlabels F:."""
    path = tmp_path / "drive_names.json"
    DriveNames(path).set("F:", "Top drive")
    DriveNames(path).set("G:", "Bottom drive")  # a fresh instance, as after a restart

    reloaded = DriveNames(path)
    assert reloaded.get("F:") == "Top drive"
    assert reloaded.get("G:") == "Bottom drive"


def test_clearing_a_label_forgets_the_drive_rather_than_storing_a_blank(tmp_path):
    """The UI clears a label by submitting an empty box. A stored "" would show
    as an empty label in the drive list instead of falling back to the model."""
    path = tmp_path / "drive_names.json"
    names = DriveNames(path)

    names.set("F:", "Top drive")
    names.set("F:", None)
    assert names.get("F:") is None

    names.set("F:", "Top drive")
    names.set("F:", "   ")
    assert names.get("F:") is None
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_surrounding_whitespace_is_trimmed_off_a_label(tmp_path):
    """A pasted label arrives with a trailing newline; storing it raw makes the
    saved name differ from what the user sees and re-types."""
    path = tmp_path / "drive_names.json"
    names = DriveNames(path)
    names.set("F:", "  Top drive\n")

    assert names.get("F:") == "Top drive"
    assert json.loads(path.read_text(encoding="utf-8")) == {"F:": "Top drive"}


def test_a_corrupt_names_file_costs_the_labels_not_the_app(tmp_path):
    """Half-written file (the serve VM is powered off mid-save): cosmetic labels
    must not take the UI down at start-up, and the next save must repair it."""
    path = tmp_path / "drive_names.json"
    path.write_text('{"F:": "Top dri', encoding="utf-8")

    names = DriveNames(path)
    assert names.get("F:") is None

    names.set("G:", "Bottom drive")
    assert json.loads(path.read_text(encoding="utf-8")) == {"G:": "Bottom drive"}


def test_an_unlabelled_drive_simply_reports_no_name(tmp_path):
    """get() feeds the drive list on every poll; a KeyError there blanks the
    page for drives the user never bothered to name."""
    path = tmp_path / "drive_names.json"
    assert DriveNames(path).get("F:") is None  # no file at all yet

    names = DriveNames(path)
    names.set("F:", "Top drive")
    assert names.get("E:") is None   # file exists, device unknown
    assert names.get(None) is None   # drive enumerated without a letter


# --- subtitle audit -----------------------------------------------------


def _kept(lang):  # the user keeps English and Polish
    return lang in ("eng", "pol")


def _sub(lang, sid=0):
    return Stream(stream_id=sid, stream_type=StreamType.SUBTITLE, codec="PGS", language=lang)


def _audio(lang, sid=1):
    return Stream(stream_id=sid, stream_type=StreamType.AUDIO, codec="TrueHD", language=lang)


def _title(title_id, *streams):
    return Title(title_id=title_id, streams=list(streams))


def _disc(*titles, name="LOGICAL_VOLUME_ID"):
    return DiscInfo(source="disc:0", disc_name=name, titles=list(titles))


def _rec(job_id=1, *, disc=None, selected=(0,), outputs=(), assignments=(),
         source="disc:0", state="done"):
    return JobRecord(
        id=job_id, source=source, state=state, disc=disc_to_dict(disc),
        selected_title_ids=list(selected),
        assignments=[assignment_to_dict(a) for a in assignments],
        outputs=list(outputs))


def _audit_one(record, list_sidecars):
    return audit_done_jobs([record], _kept, list_sidecars)[0]


def test_only_the_selected_titles_decide_whether_subs_were_expected():
    """Discs are full of bonus titles we never rip, and every title carries an
    English audio track. Looking at anything but the selected titles' SUBTITLE
    streams would flag finished movies that were never supposed to have an .srt."""
    disc = _disc(_title(0, _audio("eng")), _title(1, _sub("eng")))
    no_sidecars = lambda _video: []

    main = _audit_one(_rec(disc=disc, selected=[0], outputs=["/lib/A.mkv"]), no_sidecars)
    extra = _audit_one(_rec(disc=disc, selected=[1], outputs=["/lib/A.mkv"]), no_sidecars)

    assert main["expected"] is False and main["status"] == "ok"
    assert extra["expected"] is True and extra["status"] == "missing"


def test_a_done_job_that_placed_nothing_is_unverifiable_not_healthy():
    """A DONE job with no outputs has no video to look beside. Calling it "ok"
    hides a job that finished without ever producing a file."""
    disc = _disc(_title(0, _sub("eng")))
    row = _audit_one(_rec(disc=disc, outputs=[]), lambda _video: ["/lib/stray.srt"])

    assert row["status"] == "unreachable"
    assert row["outputs"] == 0 and row["srts"] == 0


def test_one_readable_output_is_enough_to_judge_a_multi_file_job():
    """A boxset places several episodes. If one of them lives on a share this
    session can't see, the ones it CAN see still have to be checked — and an
    unreachable sibling must not launder a genuinely missing sidecar into "ok"."""
    disc = _disc(_title(0, _sub("eng")))
    outputs = ["/lib/E01.mkv", "/lib/E02.mkv"]  # E02 is never in the map -> None

    has_srt = _audit_one(_rec(disc=disc, outputs=outputs),
                         {"/lib/E01.mkv": ["/lib/E01.en.srt"]}.get)
    no_srt = _audit_one(_rec(disc=disc, outputs=outputs), {"/lib/E01.mkv": []}.get)
    nothing_visible = _audit_one(_rec(disc=disc, outputs=outputs), lambda _video: None)

    assert has_srt["status"] == "ok"
    assert has_srt["srts"] == 1 and has_srt["outputs"] == 2
    assert no_srt["status"] == "missing"
    assert nothing_visible["status"] == "unreachable"  # not "missing": we didn't look


def test_the_sidecar_count_covers_every_file_in_the_job():
    """The count is what a human eyeballs to spot "13 episodes, 4 subtitles".
    Reporting only the last file's tally makes a half-OCR'd boxset look fine."""
    disc = _disc(_title(0, _sub("eng")))
    sidecars = {"/lib/E01.mkv": ["/lib/E01.en.srt"],
                "/lib/E02.mkv": ["/lib/E02.en.srt", "/lib/E02.en.sdh.srt"]}
    row = _audit_one(_rec(disc=disc, outputs=list(sidecars)), sidecars.get)

    assert row["srts"] == 3 and row["outputs"] == 2
    assert row["status"] == "ok"


def test_a_flagged_row_is_named_by_its_assigned_title():
    """The audit prints a to-do list for a human. "LOGICAL_VOLUME_ID" doesn't
    tell them which film to re-rip; the title they assigned does."""
    disc = _disc(_title(0, _sub("eng")))
    assignment = EpisodeAssignment(0, MediaKind.MOVIE, "Blade Runner", 1982)
    row = _audit_one(_rec(disc=disc, assignments=[assignment], outputs=["/lib/A.mkv"]),
                     lambda _video: [])

    assert row["name"] == "Blade Runner"
    assert row["status"] == "missing"


def test_an_unassigned_job_falls_back_to_the_disc_label_then_the_source():
    """Jobs that never got an assignment (auto-identify gave up, or the job was
    built from a file) still need something identifiable in the report."""
    rows = audit_done_jobs([
        _rec(1, disc=_disc(_title(0), name="MINORITY_REPORT"), outputs=["/lib/A.mkv"]),
        _rec(2, disc=_disc(_title(0), name=None), source="file:/mnt/rips/Weird Disc",
             outputs=["/lib/B.mkv"]),
        _rec(3, disc=None, source="disc:1", outputs=["/lib/C.mkv"]),
    ], _kept, lambda _video: [])

    assert {r["id"]: r["name"] for r in rows} == {
        1: "MINORITY_REPORT", 2: "file:/mnt/rips/Weird Disc", 3: "disc:1"}


def test_a_job_whose_disc_scan_is_gone_is_not_flagged_as_missing():
    """Jobs imported from an existing .mkv carry no disc metadata. The audit has
    to treat "we don't know what was on it" as no expectation, not as a fault —
    and certainly not as a crash that kills the whole report."""
    row = _audit_one(_rec(disc=None, outputs=["/lib/A.mkv"]), lambda _video: [])

    assert row["expected"] is False and row["status"] == "ok"


# --- OpenSubtitles upload sheet -----------------------------------------


def _movie(title="The Film", year=1999):
    return EpisodeAssignment(0, MediaKind.MOVIE, title, year)


def _sheet(**overrides):
    kwargs = dict(srt_name="The Film (1999).en.srt", assignment=_movie(),
                  language_code="eng", is_sdh=False, forced=False, height=1080,
                  fps=23.976, version="v9")
    kwargs.update(overrides)
    return os_upload.build_info_sheet(**kwargs)


def test_an_unknown_height_neither_claims_hd_nor_invents_a_resolution():
    """Height comes from probing the encode and can come back empty. Ticking the
    HD box on a guess gets the upload rejected, and a "Nonep" release name is
    unusable — the sheet is pasted into the form verbatim."""
    unknown = _sheet(height=None)
    known = _sheet(height=1080)

    assert "high-definition movie:  no" in unknown
    assert "high-definition movie:  YES" in known
    assert "(video is" not in unknown and "(video is 1080p)" in known
    assert "The.Film.1999.BluRay" in unknown
    assert "The.Film.1999.1080p.BluRay" in known


def test_the_hd_checkbox_follows_the_720_line():
    """OpenSubtitles defines HD as 720p and up. Getting the boundary wrong
    mislabels every 720p upload."""
    assert "high-definition movie:  YES" in _sheet(height=720)
    assert "high-definition movie:  no" in _sheet(height=576)


def test_an_unknown_fps_asks_for_a_manual_check_instead_of_writing_zero():
    """A wrong FPS desyncs the subtitle for everyone who downloads it. When we
    couldn't determine it, the sheet must say so rather than print a confident 0."""
    sheet = _sheet(fps=None)

    assert "FPS:                   (check the video file)" in sheet
    assert "0.000" not in sheet


def test_fps_is_written_precisely_enough_to_tell_23_976_from_24():
    """23.976 vs 24 is exactly the mismatch that drifts a subtitle a second out
    by the end of a film; rounding it away in the sheet propagates the error."""
    assert "FPS:                   23.976" in _sheet(fps=23.976)
    assert "FPS:                   25.000" in _sheet(fps=25.0)


def test_an_unlisted_language_still_produces_a_usable_sheet():
    """The name table only covers common languages, and MakeMKV's case varies.
    An unknown code must degrade to the code itself, never to an empty field."""
    assert os_upload.language_name("ENG") == "English"
    assert os_upload.language_name("kat") == "kat"
    assert os_upload.language_name(None) == "Undetermined"
    assert os_upload.language_name("") == "Undetermined"
    assert "Language:              Undetermined (und)" in _sheet(language_code=None)


def test_the_sheet_records_the_build_that_produced_the_ocr():
    """Uploads stay traceable to the version that OCR'd them, so a bad
    SubtitleEdit/Tesseract release can be found and its uploads pulled."""
    assert ("Comment:               OCR'd from disc PGS subtitles by "
            "BluRayRipper 0.1.0+242.bc2d392") in _sheet(version="0.1.0+242.bc2d392")


def test_only_a_tv_assignment_gets_an_episode_line_and_numbers_are_optional():
    """A show's extras are assigned with no season or episode. The sheet still
    has to render for them instead of blowing up formatting None — and a movie
    must not sprout an empty Episode field."""
    special = EpisodeAssignment(0, MediaKind.TV, "The Show", 2015,
                                episode_title="The Special")
    sheet = _sheet(assignment=special)

    assert "The Special" in sheet
    assert "None" not in sheet
    assert "Episode:" in sheet
    assert "Episode:" not in _sheet()  # the movie sheet


def test_the_release_name_uses_season_and_episode_for_tv_and_the_year_for_a_movie():
    """The release name is what OpenSubtitles matches downloads against. An
    episode filed under the series year matches nothing, and S2E5 has to be
    zero-padded to the S02E05 the scene (and the matcher) expects."""
    episode = EpisodeAssignment(0, MediaKind.TV, "The Show", 2015, season=2, episode=5)

    assert os_upload.suggested_release_name(episode, 720) == "The.Show.S02E05.720p.BluRay"
    assert os_upload.suggested_release_name(_movie(), 1080) == "The.Film.1999.1080p.BluRay"
    assert os_upload.suggested_release_name(_movie(year=None), None) == "The.Film.BluRay"


def test_episodes_of_a_season_share_one_export_folder():
    """The export dir gets browsed by hand before uploading; one folder per
    season rather than per episode is what makes that bearable."""
    ep5 = EpisodeAssignment(0, MediaKind.TV, "The Show", 2015, season=2, episode=5)
    ep6 = EpisodeAssignment(0, MediaKind.TV, "The Show", 2015, season=2, episode=6)

    assert os_upload.movie_folder(ep5) == "The Show - Season 2"
    assert os_upload.movie_folder(ep6) == os_upload.movie_folder(ep5)
    assert os_upload.movie_folder(_movie()) == "The Film (1999)"
    assert os_upload.movie_folder(_movie(year=None)) == "The Film"


def test_a_title_with_path_illegal_characters_still_lands_on_disk(tmp_path):
    """Colons and slashes are legal in a film title and fatal in a Windows path.
    An unsanitised folder name makes the export raise mid-pipeline."""
    srt = tmp_path / "sub.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")

    dest = os_upload.export_subtitle(
        tmp_path / "upload", srt, assignment=_movie(title='Face/Off: "Special"'),
        language_code="eng", is_sdh=False, forced=False, height=1080, fps=24.0,
        version="v1")

    assert dest.parent.name == "Face_Off_ _Special_ (1999)"
    assert dest.exists() and dest.parent.parent == tmp_path / "upload"


def test_each_exported_srt_gets_its_own_sheet_beside_it(tmp_path):
    """A film exports both a normal and an SDH track into the same folder. One
    fixed sheet name would leave whichever landed second describing the other —
    and the SDH checkbox is the field that gets the upload rejected."""
    plain = tmp_path / "The Film (1999).en.srt"
    sdh = tmp_path / "The Film (1999).en.sdh.srt"
    for path in (plain, sdh):
        path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")

    common = dict(assignment=_movie(), language_code="eng", forced=False,
                  height=1080, fps=24.0, version="v1")
    export_dir = tmp_path / "upload"
    first = os_upload.export_subtitle(export_dir, plain, is_sdh=False, **common)
    second = os_upload.export_subtitle(export_dir, sdh, is_sdh=True, **common)

    assert first.parent == second.parent
    plain_sheet = (first.parent / (plain.name + ".info.txt")).read_text(encoding="utf-8")
    sdh_sheet = (second.parent / (sdh.name + ".info.txt")).read_text(encoding="utf-8")
    assert f"Subtitle file to upload:  {plain.name}" in plain_sheet
    assert f"Subtitle file to upload:  {sdh.name}" in sdh_sheet
    assert "hearing impaired:       no" in plain_sheet
    assert "hearing impaired:       YES" in sdh_sheet
