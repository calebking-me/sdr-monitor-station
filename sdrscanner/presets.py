"""Built-in channel lists (FRS, GMRS, MURS, CB, NOAA weather, marine, ...).

Frequencies follow the published channel plans (FCC Part 95 for FRS/GMRS/MURS/CB,
ITU/USCG for marine VHF, ETSI for PMR446). Several services span more than one
RTL-SDR capture bandwidth; the channel picker shows which channels fit.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import MODE_AM, MODE_FM, ChannelConfig


@dataclass(frozen=True)
class Preset:
    key: str
    name: str
    region: str
    description: str
    channels: tuple[tuple[str, float, str, float], ...]   # (name, MHz, mode, bandwidth Hz)

    def make_channels(self, squelch_db: float = -50.0) -> list[ChannelConfig]:
        return [ChannelConfig(name=n, frequency=round(mhz * 1e6), mode=mode, bandwidth=bw,
                              squelch_db=squelch_db, ctcss_hz=None,
                              deemphasis_us=750.0 if mode == MODE_FM else 0.0)
                for n, mhz, mode, bw in self.channels]


N, W = 12_500.0, 25_000.0

_FRS_MHZ = (
    462.5625, 462.5875, 462.6125, 462.6375, 462.6625, 462.6875, 462.7125,   # 1-7
    467.5625, 467.5875, 467.6125, 467.6375, 467.6625, 467.6875, 467.7125,   # 8-14
    462.5500, 462.5750, 462.6000, 462.6250, 462.6500, 462.6750, 462.7000, 462.7250,  # 15-22
)

# CB: channel 23 is famously out of order (27.255), followed by 24 and 25.
_CB_MHZ = (
    26.965, 26.975, 26.985, 27.005, 27.015, 27.025, 27.035, 27.055, 27.065, 27.075,
    27.085, 27.105, 27.115, 27.125, 27.135, 27.155, 27.165, 27.175, 27.185, 27.205,
    27.215, 27.225, 27.255, 27.235, 27.245, 27.265, 27.275, 27.285, 27.295, 27.305,
    27.315, 27.325, 27.335, 27.345, 27.355, 27.365, 27.375, 27.385, 27.395, 27.405,
)
_CB_NAMES = {9: "CB 9 (emergency)", 19: "CB 19 (highway)"}

PRESETS: tuple[Preset, ...] = (
    Preset("frs", "FRS (Family Radio Service)", "US/CA",
           "22 FRS channels, narrowband FM. Channels 8-14 are 5 MHz above 1-7/15-22, so "
           "only one group fits in a single capture.",
           tuple((f"FRS {i}", f, MODE_FM, N) for i, f in enumerate(_FRS_MHZ, 1))),
    Preset("gmrs", "GMRS (General Mobile Radio Service)", "US",
           "GMRS simplex/repeater outputs 1-7 and 15-22 (wideband FM) and the narrowband "
           "interstitial channels 8-14.",
           tuple((f"GMRS {i}", f, MODE_FM, N if 8 <= i <= 14 else W)
                 for i, f in enumerate(_FRS_MHZ, 1))),
    Preset("murs", "MURS (Multi-Use Radio Service)", "US",
           "5 MURS channels. 151.820-151.940 are narrowband; 154.570/154.600 (Blue/Green "
           "Dot) may be wideband. They span 2.8 MHz, so not all fit at once.",
           (("MURS 1", 151.820, MODE_FM, 11_250.0), ("MURS 2", 151.880, MODE_FM, 11_250.0),
            ("MURS 3", 151.940, MODE_FM, 11_250.0), ("MURS 4 (Blue Dot)", 154.570, MODE_FM, W),
            ("MURS 5 (Green Dot)", 154.600, MODE_FM, W))),
    Preset("cb", "CB radio (40 channels, AM)", "US/International",
           "Citizens Band channels 1-40, AM, 10 kHz spacing. All 40 fit in one capture. "
           "Needs a tuner that reaches 27 MHz (R820T/R828D do).",
           tuple((_CB_NAMES.get(i, f"CB {i}"), f, MODE_AM, 8_000.0) for i, f in enumerate(_CB_MHZ, 1))),
    Preset("noaa", "NOAA Weather Radio", "US/CA",
           "The 7 NOAA/Environment Canada weather broadcast frequencies (wideband FM).",
           tuple((f"WX{i}", f, MODE_FM, W) for i, f in enumerate(
               (162.550, 162.400, 162.475, 162.425, 162.450, 162.500, 162.525), 1))),
    Preset("marine", "Marine VHF (common US channels)", "US",
           "Distress/calling, bridge-to-bridge, Coast Guard liaison and common working "
           "channels (25 kHz FM). Channel 70 (DSC data) is not included.",
           (("Marine 16 (distress)", 156.800, MODE_FM, W), ("Marine 9 (calling)", 156.450, MODE_FM, W),
            ("Marine 6 (safety)", 156.300, MODE_FM, W), ("Marine 13 (bridge)", 156.650, MODE_FM, W),
            ("Marine 22A (USCG)", 157.100, MODE_FM, W), ("Marine 12", 156.600, MODE_FM, W),
            ("Marine 14", 156.700, MODE_FM, W), ("Marine 68", 156.425, MODE_FM, W),
            ("Marine 69", 156.475, MODE_FM, W), ("Marine 71", 156.575, MODE_FM, W),
            ("Marine 72", 156.625, MODE_FM, W), ("Marine 78A", 156.925, MODE_FM, W))),
    Preset("2m", "2 m amateur simplex", "US",
           "National calling frequency 146.520 and the common 2 m FM simplex channels.",
           tuple((f"2m {f:.3f}" + (" (call)" if f == 146.52 else ""), f, MODE_FM, W) for f in
                 (146.520, 146.400, 146.430, 146.460, 146.490, 146.550, 146.580,
                  147.420, 147.450, 147.480, 147.510, 147.540, 147.570))),
    Preset("pmr446", "PMR446 (analogue, 16 channels)", "Europe",
           "Licence-free PMR446 channels 1-16, 12.5 kHz spacing, narrowband FM.",
           tuple((f"PMR {i}", 446.00625 + (i - 1) * 0.0125, MODE_FM, N) for i in range(1, 17))),
    Preset("air", "Aviation (US common frequencies)", "US",
           "Guard (emergency), air-to-air and UNICOM/MULTICOM frequencies, AM. Local tower, "
           "ground and ATIS frequencies vary by airport - add them yourself.",
           (("Guard 121.500", 121.500, MODE_AM, 8_000.0),
            ("Air-to-air 122.750", 122.750, MODE_AM, 8_000.0),
            ("Helicopter A/A 123.025", 123.025, MODE_AM, 8_000.0),
            ("MULTICOM 122.900", 122.900, MODE_AM, 8_000.0),
            ("UNICOM 122.700", 122.700, MODE_AM, 8_000.0),
            ("UNICOM 122.725", 122.725, MODE_AM, 8_000.0),
            ("UNICOM 122.800", 122.800, MODE_AM, 8_000.0),
            ("UNICOM 122.975", 122.975, MODE_AM, 8_000.0),
            ("UNICOM 123.000", 123.000, MODE_AM, 8_000.0),
            ("UNICOM 123.050", 123.050, MODE_AM, 8_000.0),
            ("UNICOM 123.075", 123.075, MODE_AM, 8_000.0))),
)

PRESETS_BY_KEY = {p.key: p for p in PRESETS}
