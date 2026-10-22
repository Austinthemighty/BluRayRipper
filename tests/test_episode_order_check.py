"""Checking that a ripped title really is the episode it was named as.

Runtime cannot do this. The episodes on the disc that prompted it run 1292,
1293, 1294 and 1296 seconds — a title ripped under the wrong number passes
every length check there is, and every file is a real, playable episode. The
only thing that can tell them apart is what the people in them say.
"""

from pathlib import Path

import pytest

from blurayripper.config import load_settings
from blurayripper.models import EpisodeAssignment, MediaKind
from blurayripper.pipeline import Job, Pipeline


class _Report:
    def __init__(self): self.events = []
    def on_event(self, m): self.events.append(m)
    def on_progress(self, *a): pass
    def checkpoint(self): pass

    @property
    def text(self): return "\n".join(self.events)


# Dialogue distinct enough that trigram overlap separates them cleanly.
SCRIPTS = {
    1: "legen wait for it dary this is going to be legendary suit up",
    2: "have you met ted he is my best friend and he loves the yellow umbrella",
    3: "the slap bet is binding and marshall will slap you across the face",
}


def _srt(text: str) -> str:
    return "\n\n".join(
        f"{i}\n00:00:{i:02d},000 --> 00:00:{i + 1:02d},000\n{line}"
        for i, line in enumerate(text.split(" and "), start=1))


def _pipe(tmp_path, monkeypatch, *, refs=None, show_id=4414):
    s = load_settings()
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path)
    pipe = Pipeline(s)
    monkeypatch.setattr(type(pipe.osub), "configured", property(lambda self: True))

    async def gather(client, sid, season, episodes, language="en"):
        return {ep: _srt(SCRIPTS[ep]) for ep in episodes} if refs is None else refs
    monkeypatch.setattr("blurayripper.pipeline.gather_references", gather)

    async def show(assignment): return show_id
    monkeypatch.setattr(pipe, "_show_tmdb_id", show)
    return pipe


def _job(episodes_by_title):
    job = Job(source="disc:0")
    job.assignments = {
        tid: EpisodeAssignment(title_id=tid, kind=MediaKind.TV,
                               show_or_movie_title="How I Met Your Mother",
                               year=2005, season=9, episode=ep)
        for tid, ep in episodes_by_title.items()}
    return job


def _ocr(tmp_path, text_by_title):
    out = {}
    for tid, text in text_by_title.items():
        p = tmp_path / f"t{tid}.srt"
        p.write_text(_srt(text), encoding="utf-8")
        out[tid] = [(p, object())]
    return out


async def test_titles_named_as_the_wrong_episodes_are_caught(tmp_path, monkeypatch):
    """The failure runtime cannot see: the disc ripped fine, every file is a
    real episode, and two of them are under each other's names."""
    pipe = _pipe(tmp_path, monkeypatch)
    job = _job({0: 1, 1: 2, 2: 3})
    # Title 1 holds episode 3's dialogue and title 2 holds episode 2's.
    ocr = _ocr(tmp_path, {0: SCRIPTS[1], 1: SCRIPTS[3], 2: SCRIPTS[2]})
    r = _Report()

    with pytest.raises(RuntimeError) as exc:
        await pipe._verify_episode_order(job, ocr, r)

    assert "not the episodes they were named as" in str(exc.value)
    assert "title 1 was named E02 but matches E03" in str(exc.value)
    assert "Nothing was placed" in str(exc.value)


async def test_a_dvd_is_checked_from_its_captions(tmp_path, monkeypatch):
    """A DVD has nothing OCR'd — its subtitles are pictures — so the check had
    nothing to compare and never ran on the discs it was written for. Its
    closed captions are text, and they catch the same swap."""
    pipe = _pipe(tmp_path, monkeypatch)
    job = _job({0: 1, 1: 2, 2: 3})
    captions = {0: SCRIPTS[1], 1: SCRIPTS[3], 2: SCRIPTS[2]}

    async def caption_text(ripped, sub_dir, report):
        return _srt(captions[int(ripped.stem[1:])])
    monkeypatch.setattr(pipe, "_caption_text", caption_text)
    sources = {tid: (tmp_path / f"t{tid}.mkv", tmp_path / "sub") for tid in captions}

    with pytest.raises(RuntimeError) as exc:
        await pipe._verify_episode_order(job, {}, _Report(), sources=sources)

    assert "title 1 was named E02 but matches E03" in str(exc.value)


async def test_correctly_named_titles_pass_and_say_their_scores(tmp_path, monkeypatch):
    """A clean disc must sail through — and still report what it found, so a
    marginal one is visible rather than merely silent."""
    pipe = _pipe(tmp_path, monkeypatch)
    job = _job({0: 1, 1: 2, 2: 3})
    ocr = _ocr(tmp_path, {0: SCRIPTS[1], 1: SCRIPTS[2], 2: SCRIPTS[3]})
    r = _Report()

    await pipe._verify_episode_order(job, ocr, r)   # no raise

    assert "named E01, best match E01" in r.text
    assert "named E03, best match E03" in r.text


async def test_it_stays_out_of_the_way_when_it_cannot_know(tmp_path, monkeypatch):
    """Every reason this check might be unable to run has to mean "carry on",
    not "fail the job". A show nobody has uploaded subtitles for, a missing key,
    an OpenSubtitles outage and a single-title disc are all ordinary."""
    job = _job({0: 1, 1: 2})
    ocr = _ocr(tmp_path, {0: SCRIPTS[1], 1: SCRIPTS[2]})

    # No API key configured.
    s = load_settings()
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path)
    plain = Pipeline(s)
    await plain._verify_episode_order(job, ocr, _Report())

    # References came back empty (nobody uploaded this show).
    pipe = _pipe(tmp_path, monkeypatch, refs={})
    await pipe._verify_episode_order(job, ocr, _Report())

    # OpenSubtitles is down.
    async def boom(*a, **k): raise RuntimeError("502 Bad Gateway")
    monkeypatch.setattr("blurayripper.pipeline.gather_references", boom)
    r = _Report()
    await pipe._verify_episode_order(job, ocr, r)
    assert "502 Bad Gateway" in r.text and "kept as named" in r.text

    # A single title cannot be swapped with anything.
    solo = _pipe(tmp_path, monkeypatch)
    await solo._verify_episode_order(_job({0: 1}), _ocr(tmp_path, {0: SCRIPTS[1]}),
                                     _Report())


async def test_an_unsure_match_is_reported_but_not_acted_on(tmp_path, monkeypatch):
    """Bad OCR and bad references both produce low scores. Objecting on those
    would fail good rips, so the check only acts when the matcher is confident
    some OTHER episode fits better."""
    pipe = _pipe(tmp_path, monkeypatch)
    job = _job({0: 1, 1: 2})
    mush = "aaaa bbbb cccc dddd eeee ffff"
    ocr = _ocr(tmp_path, {0: mush, 1: mush + " gggg"})
    r = _Report()

    await pipe._verify_episode_order(job, ocr, r)   # no raise

    assert "Subtitle check" in r.text


async def test_a_movie_disc_is_left_alone(tmp_path, monkeypatch):
    """There are no episodes to confuse, and no reference to fetch."""
    pipe = _pipe(tmp_path, monkeypatch)
    job = Job(source="disc:0")
    job.assignments = {0: EpisodeAssignment(
        title_id=0, kind=MediaKind.MOVIE, show_or_movie_title="Matilda", year=1996)}
    await pipe._verify_episode_order(job, _ocr(tmp_path, {0: SCRIPTS[1]}), _Report())
