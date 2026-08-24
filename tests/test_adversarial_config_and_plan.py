"""Hostile configuration: a box-set CSV and a settings page used badly.

Both of these are text a human types, and both of them decide what happens to a
disc hours later with nobody watching. A plan row is *consent to auto-rip* — no
naming gate, no confirmation — so a row that says something impossible must park
the disc rather than guess. A settings value is worse still: it is written once
and then read by every job on every box, so a value the form accepts but the
pipeline cannot use turns into an encode that fails at 3am, or (far worse) one
that finishes and reports success having thrown the audio away.

So the question every test here asks is not "does it raise" but "what does the
system do with it": is the row refused, is the disc parked, is the bad value
kept out of the file AND out of the cluster, and does the operator get a message
naming the cell they have to fix.

secretbox is the same question in a different key: a blob that has been
truncated, downgraded, or edited in the database must fail loudly, because the
alternative — returning no credentials — is indistinguishable from a working
cluster that simply has none configured.
"""

from pathlib import Path

import pytest

from blurayripper import boxset_plan as bp, config, settings_store as ss
from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, MediaKind, Title
from blurayripper.persistence import MemoryStore
from blurayripper.scheduler import EncodeScheduler
from blurayripper.services import secretbox


class Durable(MemoryStore):
    durable = True   # subclass attr shadows the base read-only property


class _Settings:
    """The two attributes boxset_plan.load_plan/save_plan actually read."""

    def __init__(self, config_dir):
        self.config_dir = config_dir


def _disc(label, n=4, seconds=2700):
    """A disc with n titles inside the 18-75 min episode-length window."""
    return DiscInfo(
        source="disc:0", disc_name=label, volume_label=label,
        titles=[Title(title_id=i, duration_seconds=seconds, chapter_count=6,
                      source_filename=f"{i:05d}.mpls") for i in range(n)],
    )


def _toml(cfg_dir, text=""):
    (cfg_dir / "settings.toml").write_text(text, encoding="utf-8")
    return cfg_dir


# --- box-set CSV: headers -------------------------------------------------

def test_a_header_missing_show_refuses_every_row_rather_than_leaving_them_nameless():
    """disc_label alone cannot name anything. Parsing the rows anyway would hand
    back rows whose show is "" — which reads downstream as a real title and
    would create a library folder with no name."""
    rows, errors = bp.parse_csv(
        "disc_label,season,first_episode\n"
        "HAWAII_FIVE_0_S1_D1,1,1\n"
        "HAWAII_FIVE_0_S1_D2,1,5\n"
    )

    assert rows == []
    assert any("show" in e and "Missing required column" in e for e in errors)


def test_column_order_and_header_spacing_do_not_change_what_a_row_means():
    """The header row is what makes the columns free-form, so a CSV exported by
    a spreadsheet — different order, padded, title-cased — has to mean exactly
    what the documented order means."""
    canonical, _ = bp.parse_csv(
        "disc_label,show,year,season,first_episode,episodes\n"
        "THE_OFFICE_S3_D2,The Office,2005,3,9,4\n"
    )
    shuffled, errors = bp.parse_csv(
        " Episodes , First_Episode ,SEASON,Year,Show,Disc_Label \n"
        "4,9,3,2005,The Office,THE_OFFICE_S3_D2\n"
    )

    assert errors == []
    assert len(canonical) == len(shuffled) == 1
    assert canonical[0].as_dict() == shuffled[0].as_dict()
    assert shuffled[0].season == 3 and shuffled[0].episodes == 4


def test_a_column_repeated_in_the_header_is_not_mistaken_for_a_repeated_disc():
    """A hand-edited header ends up with the same column twice. The header is
    compared as a SET of names, so the repeat is neither an unknown column nor a
    duplicate disc_label — both of which would drop the only row in the file."""
    rows, errors = bp.parse_csv(
        "disc_label,show,show,season,first_episode\n"
        "THE_OFFICE_S3_D2,The Office,The Office US,3,9\n"
    )

    assert errors == []
    assert len(rows) == 1
    assert bp.match(rows, "THE_OFFICE_S3_D2") is rows[0]


def test_a_row_of_two_hundred_columns_is_reduced_to_the_six_that_mean_something():
    """A spreadsheet export drags along every column that was ever in the sheet.
    They are named as ignored — a warning, not a refusal, since the six real
    columns are all present — and none of them may leak into a real field."""
    extra = [f"note{i}" for i in range(194)]
    header = "disc_label,show,season,first_episode,episodes," + ",".join(extra)
    values = "THE_OFFICE_S3_D2,The Office,3,9,4," + ",".join("x" for _ in extra)
    rows, errors = bp.parse_csv(header + "\n" + values + "\n")

    assert len(rows) == 1
    assert (rows[0].show, rows[0].season, rows[0].first_episode) == ("The Office", 3, 9)
    assert rows[0].year is None
    assert len(errors) == 1 and "Ignoring unknown column" in errors[0]
    assert "note0" in errors[0] and "note193" in errors[0]

    # The opposite mistake — two declared columns, two hundred values on the
    # line. The surplus must not shift into year/season and renumber episodes.
    spilled, spill_errors = bp.parse_csv(
        "disc_label,show\n"
        "THE_OFFICE_S3_D2,The Office," + ",".join("999" for _ in range(198)) + "\n"
    )
    assert len(spilled) == 1 and spill_errors == []
    assert (spilled[0].year, spilled[0].season,
            spilled[0].first_episode, spilled[0].episodes) == (None, None, None, None)


# --- box-set CSV: rows ----------------------------------------------------

def test_two_rows_for_the_same_disc_keep_the_first_however_the_label_was_typed():
    """People write the show's real title ("Hawaii Five-0 S1 D1") next to the
    disc's underscored volume label, not noticing they are the same disc. The
    second row must be reported and dropped, not silently override the first —
    the difference is visible in the episode numbers that get applied."""
    rows, errors = bp.parse_csv(
        "disc_label,show,season,first_episode\n"
        "HAWAII_FIVE_0_S1_D1,First Wins,1,1\n"
        "Hawaii Five-0 S1 D1,Second Loses,1,50\n"
    )

    assert [r.show for r in rows] == ["First Wins"]
    assert any("duplicate" in e for e in errors)

    applied = bp.assignments_for(bp.match(rows, "HAWAII_FIVE_0_S1_D1"), _disc("X", n=3))
    assert [a.episode for a in applied.assignments] == [1, 2, 3]      # not 50, 51, 52


def test_a_catch_all_wildcard_still_cannot_name_a_movie_disc():
    """`*` in disc_label matches every disc ever inserted. That is allowed — it
    is how one row covers a whole set — but it must not become a licence to
    auto-rip a film as episode 1 of something. The episode-length gate is what
    stops it, so this checks the row DOES fire on a real TV disc first."""
    rows, errors = bp.parse_csv("disc_label,show,season,first_episode\n*,Catch All,1,1\n")
    assert errors == []

    tv = bp.assignments_for(rows[0], _disc("SOME_TV_DISC", n=5))
    assert tv.ok and [a.episode for a in tv.assignments] == [1, 2, 3, 4, 5]

    movie = bp.assignments_for(rows[0], _disc("BLADE_RUNNER_2049", n=1, seconds=9000))
    assert not movie.ok
    assert movie.assignments == [] and "episode-length" in movie.reason


def test_a_disc_whose_label_is_only_punctuation_is_claimed_by_nothing():
    """Unbranded discs report labels like "____" or "-". A catch-all row must
    not treat that as a match, because there is nothing there to have matched."""
    rows, _ = bp.parse_csv("disc_label,show,season,first_episode\n*,Catch All,1,1\n")

    for junk in ("___", "-", " . ", ""):
        assert bp.match(rows, junk) is None, junk
    assert bp.match(rows, None) is None
    assert bp.match(rows, "REAL_LABEL") is rows[0]     # the row itself does work


def test_an_episode_count_of_zero_or_below_parks_the_disc_instead_of_applying():
    """`episodes` is the safety valve: it says "this disc holds exactly N". A 0
    or a negative is not "no expectation" — it is a typo, and treating it as
    unset would auto-rip a disc nobody checked the count of."""
    rows, errors = bp.parse_csv(
        "disc_label,show,season,first_episode,episodes\n"
        "ZERO_S1_D1,Zero,1,1,0\n"
        "NEG_S1_D1,Neg,1,1,-3\n"
    )
    assert errors == [] and len(rows) == 2

    for row in rows:
        result = bp.assignments_for(row, _disc(row.disc_label, n=4))
        assert not result.ok and result.assignments == []
        assert "4" in result.reason           # names the count the disc really has


def test_a_season_that_is_not_a_number_is_reported_and_the_label_still_supplies_one():
    """A junk cell is dropped rather than raising, and the row survives — but
    only because the disc label states the season itself. The season that gets
    applied has to come from the label, not from the text in the cell."""
    rows, errors = bp.parse_csv(
        "disc_label,show,season,first_episode\n"
        "HAWAII_FIVE_0_S4_D1,Hawaii Five-0,two,1\n"
    )

    assert len(rows) == 1 and rows[0].season is None
    assert any("season must be a whole number" in e and "two" in e for e in errors)

    applied = bp.assignments_for(rows[0], _disc("HAWAII_FIVE_0_S4_D1", n=3))
    assert applied.ok and applied.season == 4          # read off the label
    assert all(a.season == 4 for a in applied.assignments)


def test_an_out_of_range_year_or_first_episode_is_carried_through_not_clamped():
    """The plan is the user's statement of fact, so the numbers reach the
    library exactly as written — nothing quietly rounds a year into range or
    restarts the episode numbering at 1, which would put files somewhere the
    operator would never think to look for them."""
    rows, errors = bp.parse_csv(
        "disc_label,show,year,season,first_episode\n"
        "FUTURE_S1_D1,Future Show,99999,1,900\n"
        "NOYEAR_S1_D1,No Year Show,0,1,1\n"
    )
    assert errors == []

    future = bp.assignments_for(rows[0], _disc("FUTURE_S1_D1", n=3))
    assert future.ok
    assert [a.episode for a in future.assignments] == [900, 901, 902]
    assert {a.year for a in future.assignments} == {99999}

    # A year of 0 stays 0 rather than becoming "no year": the two are different
    # things to everything downstream (see notes on the missing-year warning).
    assert rows[1].year == 0
    zero = bp.assignments_for(rows[1], _disc("NOYEAR_S1_D1", n=2))
    assert {a.year for a in zero.assignments} == {0}


def test_a_show_name_with_embedded_quotes_and_a_newline_stays_one_row():
    """Pasting a title out of a web page brings its quoting with it. RFC-4180
    quoting means the newline is INSIDE the cell — the row after it must still
    be parsed as its own row, and the title must arrive unmangled."""
    rows, errors = bp.parse_csv(
        'disc_label,show,season,first_episode\n'
        'WEIRD_S1_D1,"The ""Office""\nUS",1,1\n'
        'PLAIN_S1_D1,Plain Show,1,1\n'
    )

    assert errors == []
    assert [r.disc_label for r in rows] == ["WEIRD_S1_D1", "PLAIN_S1_D1"]
    assert rows[0].show == 'The "Office"\nUS'
    assert rows[1].show == "Plain Show"

    applied = bp.assignments_for(bp.match(rows, "WEIRD_S1_D1"), _disc("WEIRD_S1_D1", n=2))
    assert applied.ok
    assert {a.show_or_movie_title for a in applied.assignments} == {'The "Office"\nUS'}
    assert all(a.kind is MediaKind.TV for a in applied.assignments)


def test_a_plan_the_size_of_a_whole_library_parses_and_still_matches_its_last_row():
    """A box-set collection written out disc by disc is a big file. Nothing may
    cap it: a row silently dropped past some limit is a disc that stops being
    auto-named for no visible reason."""
    body = "".join(
        f"COLLECTED_BOX_SET_NUMBER_{i:06d}_S1_D1,Collected Box Set Number {i:06d},2005,1,1,4\n"
        for i in range(125_000)
    )
    text = "disc_label,show,year,season,first_episode,episodes\n" + body
    assert len(text.encode("utf-8")) > 10_000_000        # over 10 MB, as pasted

    rows, errors = bp.parse_csv(text)

    assert errors == [] and len(rows) == 125_000
    last = bp.match(rows, "COLLECTED_BOX_SET_NUMBER_124999_S1_D1")
    assert last is not None and last.show == "Collected Box Set Number 124999"
    assert bp.match(rows, "COLLECTED_BOX_SET_NUMBER_125000_S1_D1") is None


def test_a_plan_saved_with_quoting_and_non_latin_text_reads_back_byte_for_byte(tmp_path):
    """The CSV is written by the web editor and read back by whichever box has
    the disc. A title with a newline inside a quoted cell, or one that is not
    Latin at all, has to survive that round trip or the rows silently change."""
    settings = _Settings(tmp_path / "config")
    text = (
        'disc_label,show,season,first_episode\n'
        'JP_S1_D1,"新世紀\nエヴァンゲリオン",1,1\n'
    )

    bp.save_plan(settings, text)

    assert bp.load_plan(settings) == text
    rows, errors = bp.parse_csv(bp.load_plan(settings))
    assert errors == [] and rows[0].show == "新世紀\nエヴァンゲリオン"
    assert bp.load_rows(settings)[0].show == "新世紀\nエヴァンゲリオン"


def test_no_plan_file_at_all_reads_as_no_plan_rather_than_an_error(tmp_path):
    """A box that has never had a plan written must not treat that as a fault —
    it just names discs by hand."""
    settings = _Settings(tmp_path / "never" / "created")

    assert bp.load_plan(settings) == ""
    assert bp.load_rows(settings) == []
    assert bp.parse_csv("") == ([], []) and bp.parse_csv("  \n \n") == ([], [])


# --- settings: values the form must not be able to write ------------------

def test_every_choice_field_refuses_a_value_that_is_not_one_of_its_options(tmp_path):
    """Checked across the whole schema rather than one field, because the cost
    is per-field and invisible until it runs: an unknown x265 preset or audio
    mode fails hours into an encode, and an unknown container names the output
    file wrongly. The message has to carry the valid options — the person
    reading it is looking at a form that just refused them."""
    _toml(tmp_path)
    choices = [f for _title, group in ss.SCHEMA for f in group if f.type == "choice"]
    assert len(choices) >= 5           # the loop below must actually be doing work

    for field in choices:
        with pytest.raises(ValueError) as excinfo:
            ss.save_overlay(tmp_path, {field.section: {field.key: "not-an-option"}})
        message = str(excinfo.value)
        assert f"{field.section}.{field.key}" in message
        for option in field.choices:
            assert option in message, (field.key, option)

    assert not (tmp_path / ss.OVERLAY_NAME).exists()    # nothing was written at all


def test_a_decimal_in_a_number_box_is_refused_and_never_reaches_the_cluster(tmp_path):
    """The overlay is published cluster-wide on save. A half-validated payload
    that reached Postgres would be pulled down by every other box, so the
    rejection has to happen before anything is written OR shared."""
    _toml(tmp_path)
    store = Durable()
    ss.save_overlay(tmp_path, {"encode": {"ffmpeg_crf": "16"}}, store)
    published = store.get_shared_config(ss.SHARED_KEY)
    assert published == {"encode": {"ffmpeg_crf": 16}}

    with pytest.raises(ValueError, match="whole number"):
        ss.save_overlay(tmp_path,
                        {"encode": {"ffmpeg_crf": "20", "uhd_min_height": "3.5"}}, store)

    assert store.get_shared_config(ss.SHARED_KEY) == published
    assert ss.read_overlay(tmp_path) == {"encode": {"ffmpeg_crf": 16}}
    assert load_settings(tmp_path).encode.ffmpeg_crf == 16


def test_a_rejected_worker_override_leaves_that_workers_stored_settings_alone(tmp_path):
    """Per-worker overrides live only in the shared store, so a bad save has no
    local file to fail against — the store itself must not be touched."""
    _toml(tmp_path)
    store = Durable()
    ss.save_settings(tmp_path, {"encode": {"ffmpeg_crf": "20"}},
                     scope="worker-1", store=store)
    assert store.get_worker_settings("worker-1") == {"encode": {"ffmpeg_crf": 20}}

    with pytest.raises(ValueError, match="whole number"):
        ss.save_settings(tmp_path,
                         {"encode": {"ffmpeg_preset": "slow", "ffmpeg_crf": "22.5"}},
                         scope="worker-1", store=store)

    assert store.get_worker_settings("worker-1") == {"encode": {"ffmpeg_crf": 20}}


def test_a_worker_cannot_be_handed_a_setting_that_only_the_rip_box_obeys(tmp_path):
    """Storing web.port or disc.poll_seconds against a worker would look like a
    setting that had been applied while nothing on the worker ever reads it."""
    _toml(tmp_path)
    store = Durable()

    stored = ss.save_settings(tmp_path, {
        "web": {"port": "9999"},
        "disc": {"poll_seconds": "60"},
        "serve": {"notify_url": "https://ntfy.example.com/topic"},
        "encode": {"ffmpeg_crf": "20"},
    }, scope="worker-1", store=store)

    assert stored == {"encode": {"ffmpeg_crf": 20}}
    assert store.get_worker_settings("worker-1") == {"encode": {"ffmpeg_crf": 20}}
    assert not (tmp_path / ss.OVERLAY_NAME).exists()   # the server's file is untouched
    assert load_settings(tmp_path).web_port == 8723


def test_a_bare_string_in_a_list_field_is_one_entry_not_one_per_character(tmp_path):
    """The web form posts one text box for a list. A path-map rule is a single
    entry that contains spaces, an '=' and backslashes — splitting it on
    anything but commas would break the Windows worker's Z:\\ mapping, which is
    the only reason the field exists."""
    _toml(tmp_path)
    path_rule = r"Z:\media = /mnt/TrueNas/media"

    saved = ss.save_overlay(tmp_path, {
        "storage": {"path_map": path_rule},
        "languages": {"keep": "eng"},
    })

    assert saved["storage"]["path_map"] == [path_rule]
    assert saved["languages"]["keep"] == ["eng"]
    assert load_settings(tmp_path).languages.keep == ["eng"]


def test_a_url_typed_into_a_folder_box_is_not_treated_as_a_remote_location(tmp_path,
                                                                           monkeypatch):
    """Nothing in the app can write to an http:// URL. It is stored as typed
    (see the reported finding), but it must at least resolve to an ordinary
    absolute local path that the library layout still composes onto — rather
    than a half-parsed relative path the placement step would write into the
    working directory of whatever launched the process."""
    _toml(tmp_path)
    for var in ("BLURAYRIPPER_OUTPUT_DIR", "BLURAYRIPPER_STAGING_DIR"):
        monkeypatch.delenv(var, raising=False)

    ss.save_overlay(tmp_path, {"storage": {"output_dir": "https://nas.local/library"}})
    storage = load_settings(tmp_path).storage

    assert isinstance(storage.output_dir, Path)
    assert storage.output_dir.is_absolute()
    assert storage.movies_root == storage.output_dir / "Movies"
    assert storage.tv_root == storage.output_dir / "TV Shows"


def test_a_negative_encode_budget_cannot_leave_the_queue_unable_to_start_anything(
        tmp_path, monkeypatch):
    """The settings page accepts a negative whole number (reported). What must
    hold anyway is the consumer's floor: a capacity of -4 that reached the
    scheduler unclamped would block every encode forever while the queue looked
    perfectly healthy."""
    _toml(tmp_path)
    monkeypatch.delenv("BLURAYRIPPER_DATABASE_URL", raising=False)

    ss.save_overlay(tmp_path, {"queue": {"encode_capacity": "-4"}})
    capacity = load_settings(tmp_path).queue.encode_capacity

    assert capacity == -4
    assert EncodeScheduler(capacity).capacity == 1


# --- secretbox: blobs that arrive damaged ---------------------------------

def _good_blob():
    return secretbox.encrypt("pw", {"TMDB_API_KEY": "abc123"})


def test_no_damaged_blob_ever_opens_or_quietly_reports_no_credentials():
    """Every one of these is something a real database can hand back: a row
    truncated by a failed write, a value someone edited by hand, a blob whose
    KDF cost was lowered to make it guessable. Each must raise, and none may
    return a dict — "no credentials" reads exactly like a cluster that has none
    configured, which is how a wrong passphrase goes unnoticed for weeks."""
    cases = {}

    blob = _good_blob(); blob["ct"] = blob["ct"][:20]
    cases["truncated ciphertext"] = blob
    blob = _good_blob(); blob["ct"] = ""
    cases["empty ciphertext"] = blob
    blob = _good_blob()
    edited = bytearray(secretbox._unb64(blob["ct"])); edited[0] ^= 0xFF
    blob["ct"] = secretbox._b64(bytes(edited))
    cases["edited ciphertext"] = blob
    blob = _good_blob(); blob["salt"] = blob["salt"][:8]
    cases["truncated salt"] = blob
    blob = _good_blob(); blob["nonce"] = blob["nonce"][:6]
    cases["short nonce"] = blob
    blob = _good_blob(); del blob["ct"]
    cases["missing ciphertext field"] = blob
    blob = _good_blob(); blob["ct"] = "!!! not base64 !!!"
    cases["ciphertext that is not base64"] = blob
    blob = _good_blob(); blob["params"] = {"n": 2, "r": 8, "p": 1, "dklen": 32}
    cases["downgraded kdf cost"] = blob
    blob = _good_blob(); blob["params"] = {"n": "cheap"}
    cases["kdf params that are not numbers"] = blob
    blob = _good_blob(); blob["params"] = {"n": 1 << 30, "r": 8, "p": 1, "dklen": 32}
    cases["kdf params demanding unbounded memory"] = blob

    for name, damaged in cases.items():
        with pytest.raises(secretbox.SecretsUnavailable) as excinfo:
            secretbox.decrypt("pw", damaged)
        message = str(excinfo.value)
        assert "abc123" not in message, name           # never leaks what it held
        assert secretbox.PASSPHRASE_KEY in message, name   # says what to check


def test_a_blob_from_another_version_is_refused_before_the_slow_key_derivation(
        monkeypatch):
    """Two reasons. The message must accuse the version, not the passphrase —
    those need opposite fixes, and "rotate the key everywhere" is an expensive
    wrong turn. And scrypt at these parameters is deliberately slow, so a
    malformed row must not cost that on every box at every boot."""
    good = _good_blob()
    derivations = []
    monkeypatch.setattr(secretbox, "derive_key",
                        lambda *a, **k: derivations.append(a) or b"\x00" * 32)

    unversioned = {k: v for k, v in good.items() if k != "v"}
    for damaged in ({**good, "v": 2}, {**good, "v": "1"}, {**good, "v": None},
                    unversioned, ["not", "a", "blob"], None, "a json string"):
        with pytest.raises(secretbox.SecretsUnavailable, match="Unrecognised"):
            secretbox.decrypt("pw", damaged)

    assert derivations == []


def test_an_empty_passphrase_is_told_it_is_missing_not_that_it_is_wrong():
    """"Your key differs from the box that stored these" sends someone
    comparing secrets.env across the cluster. "It is not set here" sends them to
    the one line that is actually absent."""
    blob = _good_blob()

    with pytest.raises(secretbox.SecretsUnavailable) as missing:
        secretbox.decrypt("", blob)
    with pytest.raises(secretbox.SecretsUnavailable) as wrong:
        secretbox.decrypt("some-other-key", blob)

    assert "is not set" in str(missing.value)
    assert "differing" not in str(missing.value)
    assert "differing from the box" in str(wrong.value)

    store = Durable()
    with pytest.raises(secretbox.SecretsUnavailable):
        secretbox.publish(store, "", {"TMDB_API_KEY": "abc"})
    assert store.get_shared_config(secretbox.SHARED_KEY) is None


def test_a_blob_the_box_cannot_open_stops_it_but_an_unreachable_database_does_not():
    """The pair is the whole design. An outage is temporary and must degrade to
    this box's own secrets.env; a blob that will not open is a misconfiguration
    that has to be fixed, and degrading through it would look identical to
    having configured nothing."""
    store = Durable()
    secretbox.publish(store, "pw", {"TMDB_API_KEY": "abc123"})
    blob = store.get_shared_config(secretbox.SHARED_KEY)
    edited = bytearray(secretbox._unb64(blob["ct"]))
    edited[-1] ^= 0x01
    blob["ct"] = secretbox._b64(bytes(edited))
    store.set_shared_config(secretbox.SHARED_KEY, blob)

    with pytest.raises(secretbox.SecretsUnavailable):
        secretbox.fetch(store, "pw")

    class Unreachable(Durable):
        def get_shared_config(self, key):
            raise ConnectionError("no route to host")

    assert secretbox.fetch(Unreachable(), "pw") == {}


def test_an_unopenable_cluster_blob_stops_startup_instead_of_dropping_the_api_keys(
        tmp_path, monkeypatch):
    """The end of that argument, at the point it matters. If load_settings
    swallowed this, `serve` would come up looking healthy with tmdb_api_key set
    to None — and the first sign would be posters quietly not downloading."""
    store = Durable()
    secretbox.publish(store, "the-right-key", {"TMDB_API_KEY": "abc123"})

    config._CLUSTER_SECRETS.clear()
    monkeypatch.setattr("blurayripper.persistence.make_store", lambda url: store)
    for var in ("TMDB_API_KEY", "OPENSUBTITLES_API_KEY", "BLURAYRIPPER_DATABASE_URL",
                secretbox.PASSPHRASE_KEY):
        monkeypatch.delenv(var, raising=False)
    _toml(tmp_path)
    (tmp_path / "secrets.env").write_text(
        "BLURAYRIPPER_DATABASE_URL=postgresql://x/y\n"
        f"{secretbox.PASSPHRASE_KEY}=a-different-key\n", encoding="utf-8")

    try:
        with pytest.raises(secretbox.SecretsUnavailable, match="differing from the box"):
            load_settings(tmp_path)
    finally:
        config._CLUSTER_SECRETS.clear()
