"""Auditing finished jobs for missing subtitle sidecars."""

from blurayripper.models import DiscInfo, Stream, StreamType, Title
from blurayripper.persistence import JobRecord, disc_to_dict
from blurayripper.subtitle_audit import audit_done_jobs


def _kept(lang):  # keep English + Polish
    return lang in ("eng", "pol")


def _disc_with_subs(langs):
    streams = [Stream(stream_id=i, stream_type=StreamType.SUBTITLE, codec="PGS", language=l)
               for i, l in enumerate(langs)]
    return DiscInfo(source="disc:0", disc_name="X",
                    titles=[Title(title_id=0, streams=streams)])


def _rec(jid, langs, outputs, state="done"):
    return JobRecord(id=jid, source="disc:0", state=state,
                     disc=disc_to_dict(_disc_with_subs(langs)),
                     selected_title_ids=[0], outputs=outputs)


def test_flags_missing_only_when_kept_subs_expected():
    # sidecar map: which .srt files "exist" next to each video
    fs = {
        "/lib/HasSubs.mkv": ["/lib/HasSubs.en.srt"],   # good
        "/lib/NoSubs.mkv": [],                          # movie has none…
        "/lib/NoSubs2.mkv": [],                         # …and this one too
    }
    list_sidecars = lambda v: fs.get(v)  # None if unknown path (unreachable)

    records = [
        _rec(1, ["eng"], ["/lib/HasSubs.mkv"]),          # kept subs + has srt -> ok
        _rec(2, ["eng"], ["/lib/NoSubs.mkv"]),           # kept subs + no srt  -> missing
        _rec(3, ["fra"], ["/lib/NoSubs2.mkv"]),          # no KEPT subs on disc -> ok (none expected)
        _rec(4, ["eng"], ["/lib/Gone.mkv"]),             # path not in fs -> unreachable
        _rec(5, ["eng"], ["/lib/HasSubs.mkv"], state="failed"),  # not done -> skipped
    ]
    out = {r["id"]: r for r in audit_done_jobs(records, _kept, list_sidecars)}

    assert 5 not in out                       # only DONE jobs
    assert out[1]["status"] == "ok"
    assert out[2]["status"] == "missing" and out[2]["expected"] is True
    assert out[3]["status"] == "ok" and out[3]["expected"] is False
    assert out[4]["status"] == "unreachable"
