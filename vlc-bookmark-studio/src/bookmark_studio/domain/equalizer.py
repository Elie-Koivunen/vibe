"""Equalizer settings: VLC's 10-band equalizer with a preamp, and VLC's own presets.

The settings belong to the player, not to a bookmark. Band gains are in dB, index 0 is
the lowest band. Which frequencies the bands sit at depends on the player: a VLC window
uses VLC's classic bands (60 Hz ... 16 kHz), the in-app libVLC player the ISO octave bands
(31 Hz ... 16 kHz); the gains and presets are the same for both.

VLC's neutral preamp is +12 dB, not 0: its equalizer attenuates the dry signal by 12 dB
and the preamp makes up for it. "Flat" is therefore (12, all bands 0).
"""
from __future__ import annotations

from dataclasses import dataclass, field

BAND_COUNT = 10
MIN_DB = -20.0
MAX_DB = 20.0
NEUTRAL_PREAMP_DB = 12.0

# A VLC window's bands (VLC's default) and libVLC's (ISO); labels only.
VLC_BANDS_HZ: tuple[float, ...] = (60, 170, 310, 600, 1000, 3000, 6000, 12000, 14000, 16000)
ISO_BANDS_HZ: tuple[float, ...] = (31.25, 62.5, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)

# VLC's presets, in VLC's order: name -> (preamp dB, band gains dB). Read from libVLC
# 3.0 (libvlc_audio_equalizer_new_from_preset); a VLC window's "setpreset" gives the same.
PRESETS: dict[str, tuple[float, tuple[float, ...]]] = {
    "Flat": (12.0, (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)),
    "Classical": (12.0, (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, -7.2, -7.2, -7.2, -9.6)),
    "Club": (6.0, (0.0, 0.0, 8.0, 5.6, 5.6, 5.6, 3.2, 0.0, 0.0, 0.0)),
    "Dance": (5.0, (9.6, 7.2, 2.4, 0.0, 0.0, -5.6, -7.2, -7.2, 0.0, 0.0)),
    "Full bass": (5.0, (-8.0, 9.6, 9.6, 5.6, 1.6, -4.0, -8.0, -10.4, -11.2, -11.2)),
    "Full bass and treble": (4.0, (7.2, 5.6, 0.0, -7.2, -4.8, 1.6, 8.0, 11.2, 12.0, 12.0)),
    "Full treble": (3.0, (-9.6, -9.6, -9.6, -4.0, 2.4, 11.2, 16.0, 16.0, 16.0, 16.8)),
    "Headphones": (4.0, (4.8, 11.2, 5.6, -3.2, -2.4, 1.6, 4.8, 9.6, 12.8, 14.4)),
    "Large Hall": (5.0, (10.4, 10.4, 5.6, 5.6, 0.0, -4.8, -4.8, -4.8, 0.0, 0.0)),
    "Live": (7.0, (-4.8, 0.0, 4.0, 5.6, 5.6, 5.6, 4.0, 2.4, 2.4, 2.4)),
    "Party": (6.0, (7.2, 7.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 7.2, 7.2)),
    "Pop": (6.0, (-1.6, 4.8, 7.2, 8.0, 5.6, 0.0, -2.4, -2.4, -1.6, -1.6)),
    "Reggae": (8.0, (0.0, 0.0, 0.0, -5.6, 0.0, 6.4, 6.4, 0.0, 0.0, 0.0)),
    "Rock": (5.0, (8.0, 4.8, -5.6, -8.0, -3.2, 4.0, 8.8, 11.2, 11.2, 11.2)),
    "Ska": (6.0, (-2.4, -4.8, -4.0, 0.0, 4.0, 5.6, 8.8, 9.6, 11.2, 9.6)),
    "Soft": (5.0, (4.8, 1.6, 0.0, -2.4, 0.0, 4.0, 8.0, 9.6, 11.2, 12.0)),
    "Soft rock": (7.0, (4.0, 4.0, 2.4, 0.0, -4.0, -5.6, -3.2, 0.0, 2.4, 8.8)),
    "Techno": (5.0, (8.0, 5.6, 0.0, -5.6, -4.8, 0.0, 8.0, 9.6, 9.6, 8.8)),
}

_MATCH_TOLERANCE_DB = 0.05


def clamp_db(value: float) -> float:
    return round(max(MIN_DB, min(MAX_DB, float(value))), 1)


def _flat_bands() -> tuple[float, ...]:
    return (0.0,) * BAND_COUNT


@dataclass(frozen=True)
class EqualizerSettings:
    enabled: bool = False
    preamp_db: float = NEUTRAL_PREAMP_DB
    bands_db: tuple[float, ...] = field(default_factory=_flat_bands)

    def __post_init__(self) -> None:
        bands = tuple(clamp_db(b) for b in self.bands_db)[:BAND_COUNT]
        bands += (0.0,) * (BAND_COUNT - len(bands))
        object.__setattr__(self, "bands_db", bands)
        object.__setattr__(self, "preamp_db", clamp_db(self.preamp_db))
        object.__setattr__(self, "enabled", bool(self.enabled))

    @classmethod
    def from_preset(cls, name: str, *, enabled: bool = True) -> "EqualizerSettings":
        preamp, bands = PRESETS[name]
        return cls(enabled=enabled, preamp_db=preamp, bands_db=bands)

    def with_band(self, index: int, db: float) -> "EqualizerSettings":
        bands = list(self.bands_db)
        bands[index] = db
        return EqualizerSettings(self.enabled, self.preamp_db, tuple(bands))

    def with_preamp(self, db: float) -> "EqualizerSettings":
        return EqualizerSettings(self.enabled, db, self.bands_db)

    def with_enabled(self, enabled: bool) -> "EqualizerSettings":
        return EqualizerSettings(enabled, self.preamp_db, self.bands_db)

    def matching_preset(self) -> str | None:
        """The preset these gains are (within rounding), or None for custom settings."""
        for name, (preamp, bands) in PRESETS.items():
            if abs(preamp - self.preamp_db) <= _MATCH_TOLERANCE_DB and all(
                abs(a - b) <= _MATCH_TOLERANCE_DB for a, b in zip(bands, self.bands_db)
            ):
                return name
        return None


def band_label(hz: float) -> str:
    """60 -> "60", 1000 -> "1k", 12000 -> "12k", 31.25 -> "31"."""
    if hz >= 1000:
        k = hz / 1000
        return f"{k:g}k"
    return f"{int(hz)}"
