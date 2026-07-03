"""Reference-SRT cache: a cache hit returns without any network call, so it
costs no OpenSubtitles /download quota."""

from blurayripper.services.opensubtitles import OpenSubtitlesClient


async def test_cache_hit_returns_without_network(tmp_path):
    client = OpenSubtitlesClient("key", cache_dir=tmp_path / "os")
    p = client._cache_path(123, 1, 5, "en")
    p.parent.mkdir(parents=True)
    p.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")
    # No httpx client is constructed on a hit, so this can't hit the network.
    text = await client.fetch_episode_srt(123, 1, 5, "en")
    assert "Hi" in text


def test_cache_path_none_without_dir():
    assert OpenSubtitlesClient("key")._cache_path(1, 1, 1, "en") is None
