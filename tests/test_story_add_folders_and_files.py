"""User story: "I have a pile of MKVs already ripped and I do not want to name
them one by one."

The shelf is a NAS folder of .mkv files somebody already ripped — sometimes one
show's season, sometimes a heap of unrelated films. Adding it has to do the
right thing for each, because the two want opposite treatment:

  * a season folder must become ONE job, named once, so the show/year/season is
    typed once instead of ten times;
  * a folder of unrelated films must stay one job PER file, because each needs
    its own name (and separate jobs also encode in parallel across workers).

Only the external TOOLS are stubbed — makemkvcon, HandBrake, mkvmerge/
mkvextract, the OCR tool and TMDB's HTTP. The JobManager, the Pipeline, the
mkv_folder heuristic, MemoryStore and the naming/library/nfo services are the
real objects, so what these tests pin is what the whole chain does with a
folder: the grouping decision, the order it puts the episodes in, the fact that
already-ripped files are re-used rather than re-ripped, and the two ways the
grouping can go dangerously wrong (grouping Rocky 2/3/4 as one "show", or
failing to group and asking for the same name ten times).

`ripped` staying empty is the load-bearing recorder here: these files already
exist, so makemkvcon must never run. If the rip checkpoint stops being
pre-seeded, the pipeline reaches for a disc that was never involved.
"""

import asyncio
import time
from pathlib import Path

import pytest

import blurayripper.pipeline as pipeline_mod
import blurayripper.web.manager as manager_mod
from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Title
from blurayripper.persistence import MemoryStore
from blurayripper.pipeline import JobState
from blurayripper.tools.mkvtools import ProbedFile
from blurayripper.web.manager import JobManager

EPISODE_SECONDS = 22 * 60      # inside discinfo's 18-75 min episode window
FEATURE_SECONDS = 112 * 60     # a film: past the movie threshold

SHOW = "Rick and Morty"
SHOW_YEAR = 2013
# What MakeMKV wrote when the season was ripped: one shared stem, a _tNN counter.
# The counters start at 4 because MakeMKV numbers by disc structure, not by episode.
SEASON_FILES = [f"Rick and Morty Season 1_t{n:02d}.mkv" for n in (4, 5, 6, 10)]
FILMS = ["Black Hawk Down (2001).mkv", "Bumblebee (2018).mkv", "Cars 3 (2017).mkv"]


class Shelf:
    """A serve box plus the folder of already-ripped files it is pointed at.

    The recorders are what the assertions read: `ripped` must stay EMPTY (these
    files exist; makemkvcon has no business running), and `encodes` records
    (source filename -> placed filename) so the episode ORDER can be checked
    against the files themselves rather than against ids the test invented.
    """

    def __init__(self, manager, settings, tmp_path, ripped, scanned, encodes, runtimes):
        self.manager = manager
        self.settings = settings
        self.tmp_path = tmp_path
        self.ripped = ripped        # makemkvcon rip_title calls — expected: none
        self.scanned = scanned      # makemkvcon scan calls (source selectors)
        self.encodes = encodes      # (source .mkv name, destination name)
        self.runtimes = runtimes    # {file: seconds} the stubbed mkvmerge reports

    # --- the shelf ---------------------------------------------------------

    def folder(self, name, files, seconds=EPISODE_SECONDS):
        """Create a folder of .mkv files as if somebody had ripped them there,
        each `seconds` long when the stubbed mkvmerge probes it."""
        folder = self.tmp_path / "shelf" / name
        folder.mkdir(parents=True, exist_ok=True)
        for f in files:
            (folder / f).write_bytes(b"ORIGINAL-RIP" + b"x" * 8180)
            self.runtimes[str(folder / f)] = seconds
        return folder

    # --- what the operator does -------------------------------------------

    def add(self, source):
        return self.manager.add_source(str(source))

    def card(self, job_id):
        """The job card the browser renders — the real _job_dict."""
        return self.manager._job_dict(self.manager.jobs[job_id])

    def rows(self, job_id):
        """The title rows shown in the naming form, in the order shown."""
        return [row["playlist"] for row in self.card(job_id)["titles"]]

    def name_the_season(self, job_id, show=SHOW, year=SHOW_YEAR, season=1, first_episode=1):
        """"Fill in order": one show, one season, one starting episode, applied
        down the form's rows. The test supplies no ordering of its own — the row
        order comes from the job, which is the thing under test."""
        card = self.card(job_id)
        self.manager.set_mapping(job_id, [
            EpisodeAssignment(title_id=row["id"], kind=MediaKind.TV,
                              show_or_movie_title=show, year=year, season=season,
                              episode=first_episode + i)
            for i, row in enumerate(card["titles"])
        ])

    def name_the_film(self, job_id, title, year):
        card = self.card(job_id)
        self.manager.set_mapping(job_id, [
            EpisodeAssignment(title_id=row["id"], kind=MediaKind.MOVIE,
                              show_or_movie_title=title, year=year)
            for row in card["titles"]
        ])

    # --- what landed -------------------------------------------------------

    def placed(self):
        """Every video file in the library, relative to the library root."""
        root = self.settings.storage.output_dir
        return sorted(
            str(p.relative_to(root)).replace("\\", "/")
            for p in root.rglob("*.mkv")
        )

    def episode_of(self, source_name):
        """The library file the given shelf file ended up as."""
        for src, dst in self.encodes:
            if src == source_name:
                return dst
        return None


@pytest.fixture
def shelf(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.toml").write_text("", encoding="utf-8")
    settings = load_settings(tmp_path / "config")
    settings.config_dir = tmp_path / "config"
    settings.storage.output_dir = tmp_path / "library"
    settings.storage.staging_dir = tmp_path / "staging"
    settings.encode.container = "mkv"
    settings.cluster.use_workers = False
    settings.database_url = None
    settings.notify_url = None      # no pushes out of the test
    settings.tmdb_api_key = None    # no network; names come from the form
    settings.auto_retry_max = 0     # a failure stays failed instead of sleeping

    ripped, scanned, encodes, runtimes = [], [], [], {}

    manager = JobManager(settings, store=MemoryStore())

    # --- makemkvcon: must never rip anything on this journey ---------------
    async def fake_rip(source, title_id, dest_dir, *, on_progress=None, on_message=None):
        ripped.append((source, title_id))
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = dest_dir / f"title_t{title_id:02d}.mkv"
        out.write_bytes(b"RE-RIPPED")
        return [out]

    async def fake_scan(source, *, on_message=None):
        scanned.append(source)
        return DiscInfo(
            source=source, disc_name="RIPPED_DISC_FOLDER",
            titles=[Title(title_id=0, duration_seconds=FEATURE_SECONDS,
                          source_filename="00800.mpls")],
        )

    async def fake_list_drives(*, timeout=60.0):
        return []

    monkeypatch.setattr(manager.pipeline.makemkv, "rip_title", fake_rip)
    monkeypatch.setattr(manager.pipeline.makemkv, "scan", fake_scan)
    monkeypatch.setattr(manager.pipeline.makemkv, "list_drives", fake_list_drives)

    # --- mkvmerge / mkvextract --------------------------------------------
    async def fake_probe(path):
        return ProbedFile(duration_seconds=runtimes.get(str(path), EPISODE_SECONDS),
                          video_height=1080, audio_languages=["eng"], subtitles=[])

    async def fake_identify(path):
        return []          # these shelf rips carry no PGS tracks

    async def fake_height(path):
        return 1080

    monkeypatch.setattr(manager.pipeline.mkvtools, "probe", fake_probe)
    monkeypatch.setattr(manager.pipeline.mkvtools, "identify_subtitles", fake_identify)
    monkeypatch.setattr(manager.pipeline.mkvtools, "video_height", fake_height)

    # --- HandBrake ---------------------------------------------------------
    async def fake_encode(src, dst, *, on_progress=None, tune_grain=None, deinterlace=False):
        assert Path(src).exists(), "HandBrake was pointed at a file that isn't there"
        encodes.append((Path(src).name, Path(dst).name))
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"ENCODED:" + Path(src).read_bytes()[:12])
        return dst

    monkeypatch.setattr(manager.pipeline.handbrake, "encode", fake_encode)

    async def fake_notify(url, title, message, priority=None, click=None):
        return True

    monkeypatch.setattr(manager_mod, "notify", fake_notify)

    def fake_eject(device):
        raise AssertionError("nothing on the shelf came out of a drive")

    monkeypatch.setattr(pipeline_mod, "eject", fake_eject)

    return Shelf(manager, settings, tmp_path, ripped, scanned, encodes, runtimes)


async def _drain(manager, timeout=15.0):
    """Run until every background task the manager spawned has finished —
    including ones spawned by other tasks."""
    deadline = time.monotonic() + timeout
    while True:
        live = [t for t in list(manager._tasks) if not t.done()]
        if not live:
            await asyncio.sleep(0)
            if not [t for t in list(manager._tasks) if not t.done()]:
                return
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("background work did not finish in time")
        await asyncio.wait(live, timeout=remaining)


def _log(wj):
    return "\n".join(wj.log)


# --- a season folder: one job, named once ---------------------------------


async def test_a_folder_of_one_shows_episodes_becomes_one_job_named_once(shelf):
    """The reported case: a folder of MakeMKV episode files made seven separate
    jobs, each guessing a movie and each demanding its own naming form. One job
    with a title per file is the whole point — the operator types the show, the
    year and the season ONCE and every file in the folder is named from it."""
    folder = shelf.folder("rick and morty s1", SEASON_FILES)

    ids = shelf.add(folder)
    await _drain(shelf.manager)

    assert len(ids) == 1, "a season folder must not fan out into a job per file"
    card = shelf.card(ids[0])
    assert card["state"] == "awaiting_mapping"     # titles known; only naming left
    assert len(card["titles"]) == len(SEASON_FILES)
    assert card["disc_name"] == "Rick and Morty Season 1"   # seeds the TMDB box
    assert card["label_season"] == 1                        # pre-fills Season

    shelf.name_the_season(ids[0])                  # one form, one submit
    await _drain(shelf.manager)

    assert shelf.manager.jobs[ids[0]].state is JobState.DONE, _log(
        shelf.manager.jobs[ids[0]])
    assert shelf.placed() == [
        f"TV Shows/{SHOW} ({SHOW_YEAR})/Season 01/{SHOW} - S01E0{n}.mkv"
        for n in (1, 2, 3, 4)
    ]


async def test_the_episode_numbers_follow_the_counter_not_the_alphabet(shelf):
    """A hand-numbered set runs 8, 9, 10, 11 — and as TEXT "10" sorts before "8",
    so an alphabetical folder listing hands the operator episode 10 first and
    shifts the whole run. "Fill in order" numbers the rows in the order it is
    given them, so the counter has to decide that order all the way to the
    placed filename; a shifted season is only discovered while watching it."""
    files = [f"Poirot Series 1 - {n}.mkv" for n in (8, 9, 10, 11)]
    folder = shelf.folder("poirot s1", files)

    ids = shelf.add(folder)
    await _drain(shelf.manager)
    # The form shows them in counter order, which is what "Fill in order" walks.
    assert shelf.rows(ids[0]) == files

    shelf.name_the_season(ids[0], show="Poirot", year=1989, first_episode=8)
    await _drain(shelf.manager)

    # Read back per FILE: which shelf file became which episode.
    assert [shelf.episode_of(name) for name in files] == [
        "Poirot - S01E08.mkv", "Poirot - S01E09.mkv",
        "Poirot - S01E10.mkv", "Poirot - S01E11.mkv",
    ]


async def test_the_shelf_files_are_encoded_where_they_lie_and_never_re_ripped(shelf):
    """Each title is pre-checkpointed to the file it came from, so the pipeline
    skips ripping entirely. Lose that and makemkvcon is handed a bare filesystem
    path for a job that never involved a disc — the job dies asking for a disc
    nobody has, after the files were already on the NAS."""
    folder = shelf.folder("rick and morty s1", SEASON_FILES)

    ids = shelf.add(folder)
    await _drain(shelf.manager)
    shelf.name_the_season(ids[0])
    await _drain(shelf.manager)

    assert shelf.ripped == [], "makemkvcon ran for files that were already ripped"
    # HandBrake read the originals themselves, not a re-rip staged elsewhere.
    assert sorted(src for src, _dst in shelf.encodes) == sorted(SEASON_FILES)
    # ...and the originals are still sitting on the shelf, untouched.
    assert sorted(p.name for p in folder.glob("*.mkv")) == sorted(SEASON_FILES)
    assert (folder / SEASON_FILES[0]).read_bytes().startswith(b"ORIGINAL-RIP")
    placed = (shelf.settings.storage.output_dir / "TV Shows" /
              f"{SHOW} ({SHOW_YEAR})" / "Season 01" / f"{SHOW} - S01E01.mkv")
    assert placed.read_bytes() == b"ENCODED:ORIGINAL-RIP"   # the encode, not a copy


# --- a folder of unrelated films: one job each -----------------------------


async def test_a_folder_of_unrelated_films_stays_one_job_per_file(shelf):
    """The To Process folder must not regress. These films share nothing but a
    folder; grouping them would name Bumblebee as an episode of Black Hawk Down.
    Separate jobs also mean separate names — and parallel encodes across workers."""
    folder = shelf.folder("To Process", FILMS, seconds=FEATURE_SECONDS)

    ids = shelf.add(folder)
    await _drain(shelf.manager)

    assert len(ids) == len(FILMS)
    # Each job is its own file, with its own naming box seeded from its own name.
    assert sorted(Path(shelf.manager.jobs[i].source).name for i in ids) == sorted(FILMS)
    assert sorted(shelf.card(i)["disc_name"] for i in ids) == sorted(
        Path(f).stem for f in FILMS)
    assert all(shelf.card(i)["state"] == "awaiting_mapping" for i in ids)
    # No grouping is offered for them, so the UI never suggests the dangerous move.
    assert [shelf.card(i)["group_with"] for i in ids] == [0, 0, 0]

    # Named one card at a time — the cost this folder genuinely carries.
    typed = {"Black Hawk Down (2001)": ("Black Hawk Down", 2001),
             "Bumblebee (2018)": ("Bumblebee", 2018),
             "Cars 3 (2017)": ("Cars 3", 2017)}
    for job_id in ids:
        title, year = typed[shelf.card(job_id)["disc_name"]]
        shelf.name_the_film(job_id, title, year)
    await _drain(shelf.manager)

    assert shelf.placed() == [
        "Movies/Black Hawk Down (2001)/Black Hawk Down (2001).mkv",
        "Movies/Bumblebee (2018)/Bumblebee (2018).mkv",
        "Movies/Cars 3 (2017)/Cars 3 (2017).mkv",
    ]
    assert shelf.ripped == []


# --- folding files that were added one at a time --------------------------


async def test_files_added_one_by_one_can_be_folded_back_into_one_season_job(shelf):
    """Files dragged in individually — or added before grouping existed — sit as N
    jobs all wanting the same show name typed N times. The offer to fold them
    into one job is the escape hatch, and it has to survive the whole way: one
    naming form afterwards, and a correctly numbered season out of it."""
    folder = shelf.folder("poirot s1", [f"Poirot Series 1_t{n:02d}.mkv" for n in (1, 2, 3)])
    singles = [shelf.add(f)[0] for f in sorted(folder.glob("*.mkv"))]
    await _drain(shelf.manager)

    assert len(singles) == 3                       # three jobs, three naming forms
    # Every one of them offers the fold, so the button is there whichever card
    # the operator is looking at.
    assert [shelf.card(i)["group_with"] for i in singles] == [3, 3, 3]

    grouped_id = await shelf.manager.group_season(singles[1])
    await _drain(shelf.manager)

    assert set(shelf.manager.jobs) == {grouped_id}, "the originals must be retired"
    assert len(shelf.card(grouped_id)["titles"]) == 3
    assert all(str(i) in _log(shelf.manager.jobs[grouped_id]) for i in singles)

    shelf.name_the_season(grouped_id, show="Poirot", year=1989)
    await _drain(shelf.manager)

    assert shelf.placed() == [
        f"TV Shows/Poirot (1989)/Season 01/Poirot - S01E0{n}.mkv" for n in (1, 2, 3)]
    # Grouping is a queue operation: it never touched the files themselves.
    assert len(list(folder.glob("*.mkv"))) == 3
    assert shelf.ripped == []


async def test_the_fold_is_only_offered_to_siblings_in_the_same_folder(shelf):
    """The offer says "these files are one show". Two seasons living in two
    folders are not one job, and folding them would put season 2's episodes into
    season 1's numbering — so the candidate search is scoped to the folder."""
    s1 = shelf.folder("Show/s1", [f"Show Season 1_t{n:02d}.mkv" for n in (1, 2)])
    s2 = shelf.folder("Show/s2", [f"Show Season 2_t{n:02d}.mkv" for n in (1, 2)])
    ids = [shelf.add(f)[0] for f in sorted(s1.glob("*.mkv")) + sorted(s2.glob("*.mkv"))]
    await _drain(shelf.manager)

    assert [shelf.card(i)["group_with"] for i in ids] == [2, 2, 2, 2]  # 2, not 4

    grouped_id = await shelf.manager.group_season(ids[0])
    await _drain(shelf.manager)

    # Only season 1's two jobs were folded; season 2's are still sitting there.
    assert set(shelf.manager.jobs) == {ids[2], ids[3], grouped_id}
    assert shelf.card(grouped_id)["disc_name"] == "Show Season 1"
    assert shelf.rows(grouped_id) == ["Show Season 1_t01.mkv", "Show Season 1_t02.mkv"]


async def test_numbered_sequels_are_never_folded_into_one_show(shelf):
    """The dangerous false positive. Rocky 2/3/4 share a stem and end in a number,
    so a naive counter rule reads them as one show and names Rocky III as episode
    3 of "Rocky" — three films lost into one folder. A bare space before a number
    is not a counter, so nothing is offered and a direct request is refused."""
    folder = shelf.folder("rocky", ["Rocky 2.mkv", "Rocky 3.mkv", "Rocky 4.mkv"],
                          seconds=FEATURE_SECONDS)

    ids = shelf.add(folder)
    await _drain(shelf.manager)

    assert len(ids) == 3                                    # three films, three jobs
    assert [shelf.card(i)["group_with"] for i in ids] == [0, 0, 0]

    with pytest.raises(ValueError) as exc:
        await shelf.manager.group_season(ids[0])

    assert "one show's episodes" in str(exc.value)
    assert "Name them one at a time" in str(exc.value)
    assert set(shelf.manager.jobs) == set(ids)              # nothing was retired
    assert sorted(shelf.card(i)["disc_name"] for i in ids) == ["Rocky 2", "Rocky 3", "Rocky 4"]


# --- the folders that are not a pile of MKVs ------------------------------


async def test_a_folder_with_no_mkvs_names_the_folder_it_searched(shelf):
    """A folder holding no .mkv is a typo'd path or the wrong level of the tree.
    Saying which folder was searched is the difference between "I pointed at the
    show folder instead of the season folder" and a shrug."""
    folder = shelf.folder("Season 1", [])
    (folder / "readme.txt").write_text("not a video", encoding="utf-8")
    (folder / "Show - S01E01.mp4").write_bytes(b"wrong container")

    with pytest.raises(ValueError) as exc:
        shelf.add(folder)

    assert "No .mkv files found" in str(exc.value)
    assert str(folder) in str(exc.value)
    assert shelf.manager.jobs == {}          # no half-made job left behind
    assert shelf.scanned == []               # and MakeMKV was never asked


async def test_a_ripped_disc_folder_goes_to_makemkv_instead_of_the_mkv_route(shelf):
    """A BDMV/VIDEO_TS folder is a disc somebody copied to the NAS, not a pile of
    files: it has no .mkv at all, so the "no .mkv files found" path would reject
    it. It must be handed to MakeMKV as a file: source and scanned like a disc."""
    bdmv = shelf.tmp_path / "shelf" / "WARGAMES_DISC"
    (bdmv / "BDMV" / "STREAM").mkdir(parents=True)
    video_ts = shelf.tmp_path / "shelf" / "OLD_DVD"
    (video_ts / "VIDEO_TS").mkdir(parents=True)

    ids = shelf.add(bdmv) + shelf.add(video_ts)
    await _drain(shelf.manager)

    assert shelf.scanned == [f"file:{bdmv}", f"file:{video_ts}"]
    assert [shelf.manager.jobs[i].source for i in ids] == [
        f"file:{bdmv}", f"file:{video_ts}"]
    # The titles came from MakeMKV's scan (a playlist), not from any filename.
    assert [shelf.card(i)["titles"][0]["playlist"] for i in ids] == ["00800.mpls"] * 2
    # The name comes from the scan too, not from the folder ("WARGAMES_DISC").
    assert all(shelf.card(i)["disc_name"] == "RIPPED_DISC_FOLDER" for i in ids)
    # And unlike a shelf .mkv, nothing is pre-checkpointed: this one still has to
    # be ripped out of the folder by MakeMKV.
    assert all(shelf.manager.jobs[i]._pipeline_job.checkpoints == {} for i in ids)
