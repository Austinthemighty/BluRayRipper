"""Story: a 20-disc TV box set that names itself.

The deal the box-set plan makes with the user is "write the show, the year, the
season and the episode numbers ONCE, in a CSV, then just feed discs in". These
tests drive that from the outside — a plan file on disk, a disc inserted, the
JobManager scanning it — because the promise only holds if every step in the
chain (label match -> season/episode inference -> TMDB show pick -> rip) holds.

Two failures are worth more than the rest, and both are quiet:
  * the wrong SHOW. "Hawaii Five-O" is two series, 1968 and 2010, and TMDB ranks
    the remake first. A whole season of the original ends up carrying the
    remake's episode titles and landing in the remake's Plex folder.
  * the wrong NUMBERS. One unexpected title on a disc shifts every episode after
    it, and a shifted season is only discovered while watching it.
Both are worse than stopping, so the tests below insist on stopping.
"""

import asyncio

import pytest

from blurayripper import boxset_plan
from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, MediaKind, Title
from blurayripper.pipeline import Job, JobState, TitleResult
from blurayripper.services import metadata
from blurayripper.web.manager import JobManager

PLAN_HEADER = "disc_label,show,year,season,first_episode,episodes\n"

EPISODE_SECONDS = 47 * 60  # inside the 18-75 min episode window


def _disc(label, episodes=4, extras=()):
    """A box-set disc: `episodes` episode-length titles in playlist order, plus
    any `extras` (durations in seconds) that are not episode-length."""
    titles = [
        Title(title_id=i, duration_seconds=EPISODE_SECONDS, chapter_count=6,
              source_filename=f"{800 + i:05d}.mpls")
        for i in range(episodes)
    ]
    titles += [
        Title(title_id=900 + n, duration_seconds=seconds, chapter_count=2,
              source_filename=f"{900 + n:05d}.mpls")
        for n, seconds in enumerate(extras)
    ]
    return DiscInfo(source="disc:0", disc_name=label, volume_label=label, titles=titles)


def _settings(tmp_path):
    """A box whose config, library and staging all live under tmp_path, with
    nothing that could reach the network."""
    (tmp_path / "settings.toml").write_text("", encoding="utf-8")
    settings = load_settings(tmp_path)
    settings.config_dir = tmp_path              # where boxsets.local.csv is read from
    settings.storage.output_dir = tmp_path / "library"
    settings.storage.staging_dir = tmp_path / "staging"
    settings.database_url = None                # in-memory store
    settings.notify_url = None                  # no pushes out of the test
    settings.tmdb_api_key = None                # tests that want TMDB opt in
    return settings


class _Box:
    """The serve box: write a plan, insert discs, see what it did with them."""

    def __init__(self, settings, monkeypatch):
        self.settings = settings
        self.manager = JobManager(settings)
        self.encoded = []      # pipeline jobs that actually reached the encoder
        self._next_disc = None

        async def fake_scan(source, report):
            job = Job(source=source)
            job.state = JobState.SCANNING       # where the real scan_disc leaves it
            job.disc = self._next_disc
            job.fingerprint = f"fp_{self._next_disc.volume_label}"
            return job

        async def fake_execute(job, scheduler, report, job_name=None):
            self.encoded.append(job)
            job.results = [
                TitleResult(title_id=tid,
                            video_path=settings.storage.output_dir / f"{tid}.mkv")
                for tid in job.selected_title_ids
            ]
            return job

        monkeypatch.setattr(self.manager.pipeline, "scan_disc", fake_scan)
        monkeypatch.setattr(self.manager.pipeline, "execute", fake_execute)

    def write_plan(self, body):
        boxset_plan.save_plan(self.settings, PLAN_HEADER + body)

    async def insert(self, disc):
        """Put a disc in the drive and let the scan settle."""
        self._next_disc = disc
        wj = self.manager.add_disc("disc:0")
        for _ in range(60):
            await asyncio.sleep(0)
        return wj


@pytest.fixture
def box(tmp_path, monkeypatch):
    return _Box(_settings(tmp_path), monkeypatch)


def _episodes(wj):
    """The episode numbers this job will write, in the disc's playlist order."""
    return [wj.assignments[tid].episode for tid in wj.selected_title_ids]


def _log(wj):
    return "\n".join(wj.log)


def _show(tmdb_id, name, year):
    return metadata.ShowCandidate(tmdb_id=tmdb_id, name=name, year=year, overview="")


def _fake_tmdb(monkeypatch, shows, seasons=None):
    """Point the box at a canned TMDB. `shows` is what /search/tv returns, in
    TMDB's own popularity order (remakes usually outrank originals); `seasons`
    maps (show id, season) -> {episode number: title}."""
    seasons = seasons or {}

    class Client:
        def __init__(self, api_key, language="en-US"):
            pass

        async def search_tv(self, query):
            return list(shows)

        async def get_season(self, tv_id, season_number):
            return [
                metadata.Episode(season_number, number, name, None, "")
                for number, name in sorted(seasons.get((tv_id, season_number), {}).items())
            ]

    monkeypatch.setattr(metadata, "TMDBClient", Client)


# --- the promise -----------------------------------------------------------

async def test_one_plan_row_names_every_disc_in_the_set_with_no_form(box):
    """The entire point: 20 discs, one CSV row, no naming gate. Each disc's own
    label supplies the season and its position in the set, so if this breaks the
    user is back to typing the show and four episode numbers per disc."""
    box.write_plan("HAWAII_FIVE_0_S1_*,Hawaii Five-O,1968,,,\n")

    numbered = {}
    for label in ("HAWAII_FIVE_0_S1_D1", "HAWAII_FIVE_0_S1_D2",
                  "HAWAII_FIVE_0_S1_D4", "HAWAII_FIVE_0_S1_D5_NTSC"):
        wj = await box.insert(_disc(label, episodes=4))
        assert wj.state is JobState.DONE, _log(wj)   # never parked at a gate
        numbered[label] = _episodes(wj)

    # Disc N of a 4-per-disc set holds episodes (N-1)*4+1 onwards — including the
    # disc whose label carries an NTSC suffix, which must not hide the "D5".
    assert numbered == {
        "HAWAII_FIVE_0_S1_D1": [1, 2, 3, 4],
        "HAWAII_FIVE_0_S1_D2": [5, 6, 7, 8],
        "HAWAII_FIVE_0_S1_D4": [13, 14, 15, 16],
        "HAWAII_FIVE_0_S1_D5_NTSC": [17, 18, 19, 20],
    }
    # The names reached the pipeline, not just the web job.
    assert len(box.encoded) == 4
    assert all(
        a.kind is MediaKind.TV and a.show_or_movie_title == "Hawaii Five-O"
        and a.year == 1968 and a.season == 1
        for job in box.encoded for a in job.assignments.values()
    )


async def test_a_disc_specific_row_beats_the_wildcard_that_would_have_caught_it(box):
    """Box sets have odd discs (a short final disc, a bonus disc). One exact row
    has to be able to override the season-wide row without the user deleting the
    wildcard — and a broader wildcard must never outrank a narrower one."""
    box.write_plan(
        "HAWAII_*,Wrong Show,1968,1,1,\n"                 # too broad
        "HAWAII_FIVE_0_S1_*,Hawaii Five-O,1968,,,\n"      # the season
        "HAWAII_FIVE_0_S1_D5,Hawaii Five-O,1968,1,25,3\n"  # the odd disc out
    )

    ordinary = await box.insert(_disc("HAWAII_FIVE_0_S1_D4", episodes=4))
    odd_one = await box.insert(_disc("HAWAII_FIVE_0_S1_D5", episodes=3))

    assert _episodes(ordinary) == [13, 14, 15, 16]     # longest wildcard won
    assert _episodes(odd_one) == [25, 26, 27]          # exact row won, not 13-15
    assert {a.show_or_movie_title for a in ordinary.assignments.values()} == {"Hawaii Five-O"}
    assert {a.show_or_movie_title for a in odd_one.assignments.values()} == {"Hawaii Five-O"}


async def test_episodes_follow_playlist_order_and_skip_the_extras(box):
    """MakeMKV numbers titles by disc structure, not by broadcast order, and it
    reports the making-of alongside the episodes. Numbering titles in the order
    reported — or counting a 6-minute extra as an episode — puts every file of
    the disc in the wrong Plex slot."""
    box.write_plan("HAWAII_FIVE_0_S1_*,Hawaii Five-O,1968,,,\n")

    scrambled = _disc("HAWAII_FIVE_0_S1_D4", episodes=0, extras=(6 * 60, 2 * 3600))
    scrambled.titles += [
        Title(title_id=1, duration_seconds=EPISODE_SECONDS, source_filename="00804.mpls"),
        Title(title_id=3, duration_seconds=EPISODE_SECONDS, source_filename="00802.mpls"),
        Title(title_id=7, duration_seconds=EPISODE_SECONDS, source_filename="00801.mpls"),
        Title(title_id=9, duration_seconds=EPISODE_SECONDS, source_filename="00803.mpls"),
    ]

    wj = await box.insert(scrambled)

    # Playlist 00801 is episode 13, whatever title id MakeMKV gave it.
    assert [(tid, wj.assignments[tid].episode) for tid in wj.selected_title_ids] == [
        (7, 13), (3, 14), (9, 15), (1, 16)]
    # The 6-minute extra and the 2-hour feature are neither ripped nor numbered.
    assert 900 not in wj.assignments and 901 not in wj.assignments


# --- the wrong show --------------------------------------------------------

async def test_the_year_column_picks_the_1968_original_over_the_2010_remake(box, monkeypatch):
    """TMDB ranks the popular remake first, so taking its top hit gives a 1968
    box set the 2010 series' episode titles and Plex folder. The year the user
    typed in the CSV is the whole disambiguator and has to win."""
    box.settings.tmdb_api_key = "key"
    _fake_tmdb(
        monkeypatch,
        shows=[_show(4556, "Hawaii Five-O", 2010), _show(379, "Hawaii Five-O", 1968)],
        seasons={
            (4556, 1): {1: "Pilot", 2: "Ohana", 3: "Malama Ka Aina", 4: "Lanakila"},
            (379, 1): {1: "Cocoon", 2: "Strangers in Our Own Land",
                       3: "Tiger by the Tail", 4: "Samurai"},
        },
    )
    box.write_plan("HAWAII_FIVE_0_S1_*,Hawaii Five-O,1968,,,\n")

    wj = await box.insert(_disc("HAWAII_FIVE_0_S1_D1", episodes=4))

    assert wj.show_tmdb_id == 379                       # the original, not the top hit
    assert wj.show_year == 1968 and wj.show_title == "Hawaii Five-O"
    titles = [wj.assignments[tid].episode_title for tid in wj.selected_title_ids]
    assert titles == ["Cocoon", "Strangers in Our Own Land", "Tiger by the Tail", "Samurai"]
    assert "Pilot" not in titles                        # never the remake's season


async def test_a_year_tmdb_cannot_offer_numbers_the_episodes_rather_than_guessing(
        box, monkeypatch):
    """Same row as the test above; only TMDB's catalogue differs — the 1968 show
    is missing. Falling back to the most popular hit is exactly how a 1968 disc
    got the remake's episode titles, so it must degrade to numbers only and say
    which years it did find. The rip still goes ahead: numbers alone name the
    file correctly."""
    box.settings.tmdb_api_key = "key"
    _fake_tmdb(
        monkeypatch,
        shows=[_show(4556, "Hawaii Five-O", 2010)],
        seasons={(4556, 1): {1: "Pilot", 2: "Ohana", 3: "Malama Ka Aina", 4: "Lanakila"}},
    )
    box.write_plan("HAWAII_FIVE_0_S1_*,Hawaii Five-O,1968,,,\n")

    wj = await box.insert(_disc("HAWAII_FIVE_0_S1_D1", episodes=4))

    assert wj.show_tmdb_id is None
    assert all(a.episode_title is None for a in wj.assignments.values())
    assert _episodes(wj) == [1, 2, 3, 4] and wj.state is JobState.DONE
    assert "1968" in _log(wj) and "2010" in _log(wj)     # the years actually on offer
    assert "numbers only" in _log(wj)


async def test_two_shows_share_a_name_and_no_year_means_numbers_not_the_popular_one(
        box, monkeypatch):
    """A plan row with the year cell left blank against a name TMDB holds twice.
    Picking TMDB's first result would confidently apply the WRONG series' episode
    titles, which reads as correct until someone watches it."""
    box.settings.tmdb_api_key = "key"
    _fake_tmdb(
        monkeypatch,
        shows=[_show(4556, "Hawaii Five-O", 2010), _show(379, "Hawaii Five-O", 1968)],
        seasons={
            (4556, 1): {1: "Pilot", 2: "Ohana", 3: "Malama Ka Aina", 4: "Lanakila"},
            (379, 1): {1: "Cocoon", 2: "Strangers in Our Own Land",
                       3: "Tiger by the Tail", 4: "Samurai"},
        },
    )
    box.write_plan("HAWAII_FIVE_0_S1_*,Hawaii Five-O,,,,\n")

    wj = await box.insert(_disc("HAWAII_FIVE_0_S1_D1", episodes=4))

    assert wj.show_tmdb_id is None
    assert all(a.episode_title is None for a in wj.assignments.values())
    assert _episodes(wj) == [1, 2, 3, 4]                 # still ripped, still numbered
    assert "Add a year" in _log(wj)


async def test_a_row_with_no_year_warns_that_plex_will_be_left_guessing(box):
    """The year also names the library folder. Without it the folder is bare
    "Show", which is Plex's cue to match whichever series it likes — the same
    remake problem, one layer down. Two rows, identical but for the year cell."""
    box.write_plan(
        "HAWAII_FIVE_0_S1_*,Hawaii Five-O,1968,,,\n"
        "THE_OFFICE_S3_*,The Office,,,,\n"
    )

    pinned = await box.insert(_disc("HAWAII_FIVE_0_S1_D1", episodes=4))
    unpinned = await box.insert(_disc("THE_OFFICE_S3_D1", episodes=4))

    assert pinned.show_year == 1968
    assert "has no year" not in _log(pinned)
    assert unpinned.show_year is None
    assert all(a.year is None for a in unpinned.assignments.values())
    assert "has no year" in _log(unpinned) and "remake" in _log(unpinned)
    assert unpinned.state is JobState.DONE              # a warning, not a refusal


# --- the wrong numbers -----------------------------------------------------

async def test_an_unexpected_title_count_parks_the_disc_instead_of_shifting_numbers(box):
    """A disc with an extra episode-length title (a feature-length two-parter, a
    long extra) would silently push every episode after it up by one. The
    episodes column is the user saying how many to expect; a mismatch has to stop
    the disc, because a shifted season is only noticed while watching it."""
    box.write_plan(
        "THE_OFFICE_S3_D2,The Office,2005,3,9,4\n"
        "THE_OFFICE_S3_D3,The Office,2005,3,13,4\n"
    )

    as_expected = await box.insert(_disc("THE_OFFICE_S3_D2", episodes=4))
    one_extra = await box.insert(_disc("THE_OFFICE_S3_D3", episodes=5))

    assert _episodes(as_expected) == [9, 10, 11, 12]
    assert one_extra.state is JobState.AWAITING_TITLES  # parked for a human
    assert one_extra.assignments == {}
    assert [job.disc.volume_label for job in box.encoded] == ["THE_OFFICE_S3_D2"]
    assert "didn't apply" in _log(one_extra) and "has 5" in _log(one_extra)


async def test_a_disc_the_plan_does_not_cover_is_named_by_hand_and_says_so(box):
    """A plan that loads but doesn't match must not fall through to some other
    row — auto-ripping a disc as the wrong show costs the whole disc. And "no row
    matched" has to be distinguishable in the log from "the CSV isn't loading",
    which look identical from the outside."""
    box.write_plan("HAWAII_FIVE_0_S1_*,Hawaii Five-O,1968,,,\n")

    wj = await box.insert(_disc("THE_OFFICE_S3_D2", episodes=4))

    assert wj.state is JobState.AWAITING_TITLES
    assert wj.assignments == {} and box.encoded == []
    assert "none match the disc" in _log(wj) and "THE_OFFICE_S3_D2" in _log(wj)
    assert "1 row(s) loaded" in _log(wj)                # the CSV itself is fine


async def test_the_manual_form_still_prefills_the_season_and_disc_from_the_label(box):
    """Falling back to the form shouldn't cost the whole form. The label already
    states the season and which disc of the set this is, so the UI pre-fills both
    (the user still confirms — a mislabelled disc can't rip itself)."""
    box.write_plan("HAWAII_FIVE_0_S1_*,Hawaii Five-O,1968,,,\n")

    await box.insert(_disc("THE_OFFICE_S3_D2", episodes=4))
    await box.insert(_disc("SOME_BONUS_DISC", episodes=4))

    hints = {j["disc_name"]: (j["label_season"], j["label_disc"])
             for j in box.manager.snapshot()["jobs"]}
    assert hints["THE_OFFICE_S3_D2"] == (3, 2)
    assert hints["SOME_BONUS_DISC"] == (None, None)     # nothing to read, no guess


async def test_label_noise_is_stripped_from_a_tmdb_search_unless_the_title_is_quoted(
        tmp_path, monkeypatch):
    """Searching TMDB for the show by hand (the fallback when the plan misses)
    starts from the disc label, and TMDB matches nothing for
    "HAWAII_FIVE_0_S4_D5_NTSC". The escape hatch for a title the stripping would
    damage is double quotes, which must reach TMDB untouched — same label, only
    the quotes differ."""
    from fastapi.testclient import TestClient

    from blurayripper.web.app import create_app

    settings = _settings(tmp_path)
    settings.tmdb_api_key = "key"
    sent = []

    async def fake_get(self, path, params):
        sent.append((path, params))
        return {"results": []}

    monkeypatch.setattr(metadata.TMDBClient, "_get", fake_get)
    client = TestClient(create_app(settings))

    client.get("/api/tmdb/search", params={"kind": "tv", "query": "HAWAII_FIVE_0_S4_D5_NTSC"})
    client.get("/api/tmdb/search",
               params={"kind": "tv", "query": '"Hawaii Five-O S4 D5 NTSC"'})

    assert [path for path, _ in sent] == ["/search/tv", "/search/tv"]
    assert sent[0][1]["query"] == "HAWAII FIVE 0"        # season/disc/NTSC peeled off
    assert sent[1][1]["query"] == "Hawaii Five-O S4 D5 NTSC"   # quoted = verbatim
