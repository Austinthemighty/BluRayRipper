"""Turn ffprobe JSON into x265 HDR parameters for a UHD ffmpeg encode.

Reads the video stream's color tags plus the mastering-display / content-light
side data and formats them the way x265 wants (colorprim/transfer/colormatrix/
master-display/max-cll), so an ffmpeg+libx265 encode preserves HDR10. ffprobe
reports the mastering-display values as ``num/denom`` where the numerator is
already in the bitstream units x265 expects, so we just take the numerator.

Dolby Vision RPU is NOT handled here — a DV disc still has an HDR10 base layer,
so this yields a valid HDR10 file (minus DV's dynamic layer).
"""

from __future__ import annotations

from dataclasses import dataclass

# transfer characteristics that indicate HDR (PQ or HLG)
_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


@dataclass
class HdrInfo:
    colorprim: str | None = None      # e.g. "bt2020"
    transfer: str | None = None       # e.g. "smpte2084"
    colormatrix: str | None = None    # e.g. "bt2020nc"
    master_display: str | None = None  # x265 "G(..)B(..)R(..)WP(..)L(max,min)"
    max_cll: str | None = None        # x265 "maxcll,maxfall"

    @property
    def is_hdr(self) -> bool:
        return self.transfer in _HDR_TRANSFERS


def _num(frac) -> int | None:
    """Numerator of an ffprobe 'num/denom' string (already in x265 units)."""
    try:
        return int(str(frac).split("/")[0])
    except (ValueError, AttributeError, TypeError):
        return None


def parse_hdr(probe: dict) -> HdrInfo:
    streams = probe.get("streams") or []
    vs = next((s for s in streams if s.get("codec_type") == "video"), {})
    info = HdrInfo(
        colorprim=vs.get("color_primaries"),
        transfer=vs.get("color_transfer"),
        colormatrix=vs.get("color_space"),
    )
    frames = probe.get("frames") or []
    side = (frames[0].get("side_data_list") if frames else None) or []

    md = next((d for d in side
               if d.get("side_data_type") == "Mastering display metadata"), None)
    if md:
        g = (_num(md.get("green_x")), _num(md.get("green_y")))
        b = (_num(md.get("blue_x")), _num(md.get("blue_y")))
        r = (_num(md.get("red_x")), _num(md.get("red_y")))
        wp = (_num(md.get("white_point_x")), _num(md.get("white_point_y")))
        lmax, lmin = _num(md.get("max_luminance")), _num(md.get("min_luminance"))
        if all(v is not None for v in (*g, *b, *r, *wp, lmax, lmin)):
            info.master_display = (
                f"G({g[0]},{g[1]})B({b[0]},{b[1]})R({r[0]},{r[1]})"
                f"WP({wp[0]},{wp[1]})L({lmax},{lmin})")

    cll = next((d for d in side
                if d.get("side_data_type") == "Content light level metadata"), None)
    if cll and cll.get("max_content") is not None and cll.get("max_average") is not None:
        info.max_cll = f"{cll['max_content']},{cll['max_average']}"

    return info


def x265_params(info: HdrInfo, extra: list[str] | None = None) -> str:
    """x265-params string carrying the HDR signalling (colon-separated)."""
    parts = ["hdr-opt=1", "repeat-headers=1"]
    if info.colorprim:
        parts.append(f"colorprim={info.colorprim}")
    if info.transfer:
        parts.append(f"transfer={info.transfer}")
    if info.colormatrix:
        parts.append(f"colormatrix={info.colormatrix}")
    if info.master_display:
        parts.append(f"master-display={info.master_display}")
    if info.max_cll:
        parts.append(f"max-cll={info.max_cll}")
    if extra:
        parts.extend(extra)
    return ":".join(parts)
