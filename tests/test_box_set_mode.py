"""Box-set mode: insert a TV disc and walk away.

The label says the show and season (HOW_I_MET_YOUR_MOTHER_S1_D3), and each
title's dialogue says its episode, matched against the whole season on
OpenSubtitles. Driven from the outside, as the drive watcher would: a disc is
added, the JobManager scans it, and whatever it decides is what happens.

What must never happen is a confident wrong name: the wrong show (Hawaii
Five-O is two series), or a disc named after a guess. Every one of those
cases has to stop at the naming form and say why.
"""

import asyncio

import pytest

from blurayripper import boxset_plan
from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Title
from blurayripper.pipeline import Job, JobState, TitleResult
from blurayripper.services import metadata
from blurayripper.web.manager import JobManager, WebJob

EPISODE_SECONDS = 22 * 60


def _srt(lines):
    return "\n".join(f"{i}\n00:00:{i % 60:02d},000 --> 00:00:{(i + 1) % 60:02d},000\n{t}\n"
                     for i, t in enumerate(lines, 1))


# Made-up dialogue, one distinct run of words per episode.
_WORDS = ["lighthouse", "marmalade", "trombone", "glacier", "periscope", "velvet",
          "harbour", "cinnamon", "quarry", "lantern", "orchard", "saddle"]
DIALOGUE = {n: [f"the {_WORDS[n - 1]} {w} was never {_WORDS[-n]} again {i}"
                for i, w in enumerate(_WORDS * 3)] for n in range(1, 13)}


def _disc(label, titles=3, extras=(), movie=False):
    if movie:
        ts = [Title(title_id=0, duration_seconds=2 * 3600)]
    else:
        ts = [Title(title_id=i, duration_seconds=EPISODE_SECONDS, chapter_count=5,
                    source_filename=f"{800 + i:05d}.mpls") for i in range(titles)]
    ts += [Title(title_id=900 + n, duration_seconds=s) for n, s in enumerate(extras)]
    return DiscInfo(source="disc:0", disc_name=label, volume_label=label, titles=ts)


def _show(tmdb_id, name, year):
    return metadata.ShowCandidate(tmdb_id=tmdb_id, name=name, year=year, overview="")


HIMYM = _show(1100, "How I Met Your Mother", 2005)
SEASON = {n: f"Episode name {n}" for n in range(1, 13)}


class _Osub:
    configured = True

    def __init__(self):
        self.downloads = []

    async def fetch_episode_srt(self, tmdb_id, season, episode, lang):
        self.downloads.append((tmdb_id, season, episode))
        return _srt(DIALOGUE[episode]) if episode in DIALOGUE else None


class _Box:
    """The serve box with box-set mode on: insert discs, see what it did."""

    def __init__(self, tmp_path, monkeypatch, shows=(HIMYM,), seasons=None):
        (tmp_path / "settings.toml").write_text("", encoding="utf-8")
        settings = load_settings(tmp_path)
        settings.config_dir = tmp_path
        settings.storage.output_dir = tmp_path / "library"
        settings.storage.staging_dir = tmp_path / "staging"
        settings.database_url = None
        settings.notify_url = None
        settings.tmdb_api_key = "key"
        settings.box_set_mode = True
        self.settings = settings
        self.manager = m = JobManager(settings)
        m.osub = self.osub = _Osub()
        self.searches = []
        self.encoded = []
        self._disc = None
        self.dialogue = {}          # title id -> the episode its dialogue is
        seasons = seasons if seasons is not None else {(1100, 1): SEASON}
        box = self

        class Client:
            def __init__(self, *a, **kw): pass

            async def search_tv(self, query):
                box.searches.append(query)
                return list(shows)

            async def get_season(self, tv_id, season_number):
                return [metadata.Episode(season_number, n, name, None, "")
                        for n, name in sorted(seasons.get((tv_id, season_number), {}).items())]

        monkeypatch.setattr(metadata, "TMDBClient", Client)

        async def scan(source, report):
            job = Job(source=source)
            job.state = JobState.SCANNING
            job.disc = self._disc
            job.fingerprint = f"fp_{self._disc.volume_label}"
            return job

        async def identify(job, staging_root, report):
            return {tid: _srt(DIALOGUE[ep]) if ep else "" for tid, ep in self.dialogue.items()
                    if tid in job.selected_title_ids}

        async def execute(job, scheduler, report, job_name=None):
            self.encoded.append(job)
            job.results = [TitleResult(title_id=t, video_path=settings.storage.output_dir / f"{t}.mkv")
                           for t in job.selected_title_ids]
            return job

        monkeypatch.setattr(m.pipeline, "scan_disc", scan)
        monkeypatch.setattr(m.pipeline, "identify_phase", identify)
        monkeypatch.setattr(m.pipeline, "execute", execute)

    async def insert(self, disc, dialogue=None):
        self._disc = disc
        self.dialogue = dialogue or {}
        wj = self.manager.add_disc("disc:0")
        for _ in range(80):
            await asyncio.sleep(0)
        return wj


@pytest.fixture
def box(tmp_path, monkeypatch):
    return _Box(tmp_path, monkeypatch)


def _log(wj):
    return "\n".join(wj.log)


def _named(wj):
    return {t: a.episodes for t, a in wj.assignments.items()}


# --- the promise -----------------------------------------------------------------

async def test_a_disc_names_itself_from_its_label_and_its_dialogue(box):
    """No form at all: the label gives How I Met Your Mother season 1, and the
    dialogue gives each title its episode, out of order as discs often are."""
    wj = await box.insert(_disc("HOW_I_MET_YOUR_MOTHER_S1_D2"), {0: 5, 1: 4, 2: 6})

    assert wj.state is JobState.DONE, _log(wj)
    assert _named(wj) == {0: [5], 1: [4], 2: [6]}
    assert wj.box_set is True and box.manager._job_dict(wj)["box_set"] is True
    assert box.manager._from_record(box.manager._to_record(wj)).box_set is True
    a = wj.assignments[1]
    assert (a.show_or_movie_title, a.year, a.season) == ("How I Met Your Mother", 2005, 1)
    assert a.episode_title == "Episode name 4"
    assert wj.candidate_episodes == list(range(1, 13))       # the whole season
    assert ("Box set: How I Met Your Mother (2005) season 1, disc 2 — ripping 3 "
            "episode-length titles and identifying them by their dialogue against "
            "all 12 episodes.") in _log(wj)


async def test_it_is_off_unless_turned_on(box):
    box.settings.box_set_mode = False
    wj = await box.insert(_disc("HOW_I_MET_YOUR_MOTHER_S1_D2"), {0: 5, 1: 4, 2: 6})
    assert wj.state is JobState.AWAITING_TITLES
    assert "Box set:" not in _log(wj) and box.searches == []


async def test_a_movie_disc_is_left_alone(box):
    wj = await box.insert(_disc("WARGAMES", movie=True))
    assert wj.state is JobState.AWAITING_MAPPING
    assert "Box set:" not in _log(wj) and box.searches == []


# --- every way to be unsure goes to the form, and says why -----------------------

@pytest.mark.parametrize("label, why", [
    ("HIMYM_D3", "the label “HIMYM_D3” doesn't say which season"),
    ("SEASON_1_DISC_1", "the label “SEASON_1_DISC_1” doesn't name a show"),
])
async def test_a_label_that_does_not_say_enough_shows_the_form(box, label, why):
    wj = await box.insert(_disc(label), {0: 1, 1: 2, 2: 3})
    assert wj.state is JobState.AWAITING_TITLES
    assert f"Box set: {why} — name this one." in _log(wj)
    assert box.osub.downloads == []


async def test_without_the_keys_it_shows_the_form(box):
    box.manager.osub.configured = False
    wj = await box.insert(_disc("HOW_I_MET_YOUR_MOTHER_S1_D2"), {0: 5, 1: 4, 2: 6})
    assert wj.state is JobState.AWAITING_TITLES
    assert "it needs both an OpenSubtitles and a TMDB key" in _log(wj)


async def test_a_plan_row_for_the_disc_wins_even_one_that_did_not_apply(box):
    """The plan is what you said the disc is. A row that matched but couldn't
    be applied (here: it expects 4 titles, the disc has 3) still means "ask
    me", not "guess"."""
    boxset_plan.save_plan(box.settings,
                          "disc_label,show,year,season,first_episode,episodes\n"
                          "HOW_I_MET_YOUR_MOTHER_S1_D2,How I Met Your Mother,2005,1,5,4\n")
    wj = await box.insert(_disc("HOW_I_MET_YOUR_MOTHER_S1_D2"), {0: 5, 1: 4, 2: 6})
    assert wj.state is JobState.AWAITING_TITLES
    assert wj.box_set is False and box.osub.downloads == []


async def test_with_no_plan_the_plan_does_not_claim_it_is_naming_by_hand(box):
    wj = await box.insert(_disc("HOW_I_MET_YOUR_MOTHER_S1_D2"), {0: 5, 1: 4, 2: 6})
    assert "naming this one manually" not in _log(wj)


async def test_a_name_two_shows_share_shows_the_form(tmp_path, monkeypatch):
    """TMDB ranks the 2010 remake first. Its best guess is exactly what a 1968
    box set must not get."""
    box = _Box(tmp_path, monkeypatch,
               shows=[_show(4556, "Hawaii Five-0", 2010), _show(379, "Hawaii Five-O", 1968)],
               seasons={(379, 1): SEASON, (4556, 1): SEASON})
    wj = await box.insert(_disc("HAWAII_FIVE_0_S1_D1"), {0: 1, 1: 2, 2: 3})
    assert wj.state is JobState.AWAITING_TITLES
    assert "TMDB has 2 shows called “HAWAII FIVE 0”" in _log(wj)
    assert box.osub.downloads == []


async def test_a_name_tmdb_does_not_have_exactly_shows_the_form(tmp_path, monkeypatch):
    box = _Box(tmp_path, monkeypatch, shows=[HIMYM])
    wj = await box.insert(_disc("HIMYM_S1_D1"), {0: 1, 1: 2, 2: 3})
    assert wj.state is JobState.AWAITING_TITLES
    assert "no show called exactly “HIMYM”" in _log(wj)


# --- the set's earlier discs ------------------------------------------------------

def _named_by_hand(box, job_id, label, show, year, tmdb_id=None):
    """An earlier disc of the set, named at the form."""
    wj = WebJob(id=job_id, source="disc:1", state=JobState.DONE)
    wj.disc = _disc(label)
    wj.assignments = {0: EpisodeAssignment(0, MediaKind.TV, show, year, 1, 1)}
    wj.show_tmdb_id = tmdb_id
    box.manager.jobs[job_id] = wj
    box.manager._ids = iter(range(job_id + 1, job_id + 100))
    return wj


async def test_later_discs_follow_the_show_the_first_disc_was_given(tmp_path, monkeypatch):
    """The first Hawaii Five-O disc asks (two shows share the name); named the
    1968 one by hand, the rest of the set is that show with no more asking."""
    box = _Box(tmp_path, monkeypatch,
               shows=[_show(4556, "Hawaii Five-0", 2010), _show(379, "Hawaii Five-O", 1968)],
               seasons={(379, 1): SEASON, (4556, 1): SEASON})
    _named_by_hand(box, 50, "HAWAII_FIVE_0_S1_D1", "Hawaii Five-O", 1968, tmdb_id=379)

    wj = await box.insert(_disc("HAWAII_FIVE_0_S1_D2"), {0: 4, 1: 5, 2: 6})

    assert wj.state is JobState.DONE, _log(wj)
    assert (wj.show_title, wj.show_year, wj.show_tmdb_id) == ("Hawaii Five-O", 1968, 379)
    assert {d[0] for d in box.osub.downloads} == {379}
    assert "the same show as job 50" in _log(wj)
    assert box.searches == []                   # its TMDB id was used as it was


async def test_an_earlier_disc_named_without_a_year_cannot_settle_a_shared_name(
        tmp_path, monkeypatch):
    box = _Box(tmp_path, monkeypatch,
               shows=[_show(4556, "Hawaii Five-0", 2010), _show(379, "Hawaii Five-O", 1968)],
               seasons={(379, 1): SEASON, (4556, 1): SEASON})
    _named_by_hand(box, 50, "HAWAII_FIVE_0_S1_D1", "Hawaii Five-O", None)
    wj = await box.insert(_disc("HAWAII_FIVE_0_S1_D2"), {0: 4, 1: 5, 2: 6})
    assert wj.state is JobState.AWAITING_TITLES
    assert "TMDB has 2 shows of that name" in _log(wj)


async def test_an_earlier_disc_without_a_tmdb_id_is_looked_up_by_its_exact_name_and_year(
        tmp_path, monkeypatch):
    box = _Box(tmp_path, monkeypatch,
               shows=[_show(4556, "Hawaii Five-0", 2010), _show(379, "Hawaii Five-O", 1968)],
               seasons={(379, 1): SEASON})
    _named_by_hand(box, 50, "HAWAII_FIVE_0_S1_D1", "Hawaii Five-O", 1968)
    wj = await box.insert(_disc("HAWAII_FIVE_0_S1_D2"), {0: 4, 1: 5, 2: 6})
    assert wj.state is JobState.DONE, _log(wj)
    assert wj.show_tmdb_id == 379


async def test_an_abbreviated_label_follows_the_disc_named_by_hand(box):
    """TMDB can't read "HIMYM"; the disc before it, named at the form, can."""
    _named_by_hand(box, 60, "HIMYM_S1_D1", "How I Met Your Mother", 2005, tmdb_id=1100)
    wj = await box.insert(_disc("HIMYM_S1_D2"), {0: 4, 1: 5, 2: 6})
    assert wj.state is JobState.DONE, _log(wj)
    assert wj.show_title == "How I Met Your Mother"


async def test_earlier_discs_that_disagree_show_the_form(box):
    _named_by_hand(box, 60, "HIMYM_S1_D1", "How I Met Your Mother", 2005, tmdb_id=1100)
    _named_by_hand(box, 61, "HIMYM_S2_D1", "How I Met Your Father", 2022, tmdb_id=9)
    wj = await box.insert(_disc("HIMYM_S1_D2"), {0: 4, 1: 5, 2: 6})
    assert wj.state is JobState.AWAITING_TITLES
    assert "were named as different shows" in _log(wj)
    assert box.osub.downloads == []


async def test_a_disc_of_another_set_is_not_a_sibling(box):
    _named_by_hand(box, 60, "HAWAII_FIVE_0_S1_D1", "Hawaii Five-O", 1968, tmdb_id=379)
    wj = await box.insert(_disc("HOW_I_MET_YOUR_MOTHER_S1_D2"), {0: 5, 1: 4, 2: 6})
    assert wj.state is JobState.DONE, _log(wj)
    assert wj.show_title == "How I Met Your Mother"


async def test_the_library_folder_keeps_the_spelling_earlier_jobs_used(box):
    """Earlier discs of the show were named without a year. TMDB's
    "How I Met Your Mother (2005)" would start a second Plex folder."""
    _named_by_hand(box, 70, "SOMETHING_ELSE_S1_D1", "How I Met Your Mother", None)
    wj = await box.insert(_disc("HOW_I_MET_YOUR_MOTHER_S1_D2"), {0: 5, 1: 4, 2: 6})
    assert wj.state is JobState.DONE, _log(wj)
    assert {(a.show_or_movie_title, a.year) for a in wj.assignments.values()} == {
        ("How I Met Your Mother", None)}


async def test_a_show_of_the_same_name_from_another_year_lends_no_spelling(
        tmp_path, monkeypatch):
    box = _Box(tmp_path, monkeypatch, shows=[_show(57243, "Doctor Who", 2005)],
               seasons={(57243, 1): SEASON})
    _named_by_hand(box, 80, "DR_WHO_CLASSIC_S1_D1", "Doctor Who", 1963)
    wj = await box.insert(_disc("DOCTOR_WHO_S1_D2"), {0: 4, 1: 5, 2: 6})
    assert wj.state is JobState.DONE, _log(wj)
    assert (wj.show_title, wj.show_year) == ("Doctor Who", 2005)


# --- what the flag means afterwards -------------------------------------------------

async def test_re_opening_the_naming_or_identifying_by_hand_is_not_box_set_mode(box):
    wj = await box.insert(_disc("HOW_I_MET_YOUR_MOTHER_S1_D2"), {0: 5, 1: 4, 2: 6})
    assert wj.box_set is True
    box.manager.remap(wj.id)
    assert wj.box_set is False
    box.manager.set_auto_identify(wj.id, "How I Met Your Mother", 2005, 1100, 1,
                                  [4, 5, 6], [0, 1, 2])
    assert wj.box_set is False


def test_loose_names_meet_across_spellings():
    loose = metadata.loose_name
    assert loose("Hawaii Five-O") == loose("HAWAII FIVE 0") == loose("hawaii five-0")
    assert loose("The Office") == loose("OFFICE")
    assert loose("Law & Order") == loose("LAW AND ORDER")
    assert loose("Pokémon") == loose("POKEMON")
    assert loose("How I Met Your Mother") != loose("How I Met Your Father")


def test_same_show_returns_every_show_of_the_name_and_honours_a_year():
    shows = [_show(4556, "Hawaii Five-0", 2010), _show(379, "Hawaii Five-O", 1968),
             _show(5, "Hawaii", 2004)]
    assert [c.tmdb_id for c in metadata.same_show(shows, "HAWAII FIVE 0")] == [4556, 379]
    assert [c.tmdb_id for c in metadata.same_show(shows, "HAWAII FIVE 0", 1968)] == [379]
    assert metadata.same_show(shows, "Five-O") == []
