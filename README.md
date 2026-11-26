# SDR Multi-Channel Scanner

Monitor many FM and AM voice channels **at the same time** with a single RTL-SDR
dongle. Every channel inside the dongle's capture bandwidth (about 2 MHz at
2.4 MS/s) is demodulated in parallel. Each channel has its own squelch, CTCSS or
DCS tone squelch, volume, left/right fade and audio output device.

![Screenshot: the demo recording with channel lists, a priority channel and last-heard times](docs/screenshot.png)

![Screenshot: all 40 CB channels (AM) in the compact view](docs/screenshot-cb.png)

- **FM and AM.** Narrow (12.5 kHz) and wide (25 kHz) FM for land-mobile,
  amateur, marine, FRS/GMRS and MURS. AM for aviation and CB, with
  carrier-tracking AGC so quiet and strong stations play at the same level.
- **Preset channel lists.** One click adds FRS, GMRS, MURS, CB (all 40 AM
  channels), NOAA weather, marine VHF, 2 m amateur simplex, PMR446 or common US
  aviation frequencies.
- **CSV import/export.** Add many channels at once from a spreadsheet or a
  **CHIRP** export. Before anything is added, the picker shows which channels
  fit in the capture band.
- **Spectrum + waterfall** of the whole captured band, with mouse-wheel zoom,
  peak hold and selectable colour maps. Each channel is marked and lights up
  while its squelch is open. Double-click to add a channel; right-click for more.
- **CTCSS and DCS.** Tone squelch on all 50 CTCSS tones and all 104 DCS codes,
  in normal or inverted polarity. The tone or code each FM channel is using is
  shown live, logged, and can be adopted as its squelch with one click.
- **Channel lists.** Group channels ("Local", "Marine", "CB"…) and switch between
  them from the toolbar or with Ctrl+1…9. Only the chosen list is received, and
  the dongle retunes for it.
- **Channel strips** show a live signal meter, activity LED, detected tone/code
  and when the channel was last heard, with squelch, volume, pan, mute, solo and
  record controls. A compact one-line layout is available for long lists.
- **Organise channels in bulk.** Select several, drag them to reorder, or **edit
  any setting of many channels at once**.
- **Priority channels** turn the others down while they are active. **Alert
  channels** pop up a notification. **Temporary mute** silences a channel for
  5, 15 or 60 minutes.
- **Transmission hold.** A short drop (such as a repeater's morse ID) stays in
  one recording and one log entry instead of creating a new file each time.
- **Activity timeline** showing when each channel was active, and an
  **activity log** (with CTCSS/DCS) that exports to CSV. Double-click a log row
  to play its recording.
- **View options:** dark/light theme; spectrum above or beside the channels;
  show/hide any panel; a **streamlined view** without the menu bar, toolbar and
  tabs; a **borderless window** mode, full screen, and always-on-top (for a
  dedicated monitor).
- **Recording:** per-transmission WAV files, and raw IQ (SigMF) for playback.
- **Sources:** USB dongle, rtl_tcp, or IQ file playback (no hardware needed).

Out of scope: digital voice (P25, DMR, NXDN, D-STAR), trunking, broadcast FM,
SSB/CW, transmit.

---

## Install

### Windows: ready-made .exe (no Python needed)

1. Download `SDRScanner-<version>-win64.zip` from the repository's *Releases*
   page. Every build of `main` is also attached to its run of the
   *Windows build* workflow on the *Actions* tab.
2. Extract the zip anywhere, for example `C:\Program Files\SDRScanner` or your
   Desktop. Keep the `_internal` folder next to `SDRScanner.exe`.
3. Install the WinUSB driver with Zadig (step 3 below). It's needed once per
   dongle.
4. Run `SDRScanner.exe`.

Windows SmartScreen may warn about an unrecognised app because the exe isn't
code-signed. Click *More info → Run anyway*. You can build the exe yourself
instead; see [Building the Windows .exe](#building-the-windows-exe).

### Windows: from source

Python 3.10 or newer is required (tested with 3.12 and 3.13).

1. **Install Python** from python.org. Tick "Add python.exe to PATH".
2. **Get the code and install dependencies** (PowerShell, in the project folder):
   ```powershell
   py -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
   `pyrtlsdr[lib]` installs `pyrtlsdrlib`, which includes a prebuilt
   `librtlsdr` DLL, so most users need nothing else.
3. **Install the WinUSB driver with Zadig.** This is required once per dongle.
   Windows' default DVB-T TV driver blocks SDR access.
   1. Plug in the dongle. Download Zadig from <https://zadig.akeo.ie/> and run it.
   2. *Options → List All Devices*.
   3. Pick **Bulk-In, Interface (Interface 0)**. It may be called "RTL2838UHIDIR"
      or "RTL2832U". Do *not* pick Interface 1 or your keyboard/mouse.
   4. Check that the USB ID is `0BDA 2838` (or `0BDA 2832`), choose **WinUSB** as
      the target driver, and click **Replace Driver**.
   5. If you later plug the dongle into a different USB port, you may need to
      repeat this.
4. **Run it:** double-click `run_scanner.bat`, or run `python -m sdrscanner`.

#### Using your own librtlsdr.dll (RTL-SDR Blog V4, newer drivers)

The DLL bundled with `pyrtlsdrlib` is the classic osmocom build. **RTL-SDR Blog
V4** dongles need the RTL-SDR Blog driver fork:

1. Download the latest Windows release from
   <https://github.com/rtlsdrblog/rtl-sdr-blog/releases> (`Release.zip`, x64).
2. Copy `rtlsdr.dll` (or `librtlsdr.dll`) and `libusb-1.0.dll` from the zip
   into the folder that holds `SDRScanner.exe` (exe build) or `run_scanner.bat`
   (source checkout).

A `rtlsdr.dll`/`librtlsdr.dll` in that folder is used automatically in place of
the bundled one. You don't need to uninstall anything or change `PATH`. Only
the application's own folder is checked, never the current working directory,
so a stray DLL in your Downloads folder cannot be loaded by accident.

To check that the library loads, open *Radio settings → Detect devices*. Your
dongle should be listed. You can also run `SDRScanner.exe --self-test report.txt`
(or `python -m sdrscanner --self-test`). It reports which librtlsdr was loaded
and how many dongles it sees.

### Linux

```bash
sudo apt install librtlsdr0 libportaudio2      # Debian/Ubuntu; Fedora: rtl-sdr portaudio
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
./run_scanner.sh
```

- Stop the kernel's DVB driver from claiming the dongle:
  `echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtl.conf`,
  then unplug and replug the dongle.
- To use the dongle without root, install the udev rules (the `rtl-sdr` package
  ships them), or add your user to the `plugdev` group.
- For RTL-SDR Blog V4, install the rtl-sdr-blog driver build instead of the
  distro `librtlsdr`.

---

## Quick start without hardware

Generate a 60-second synthetic recording plus a matching profile. It has six FM
channels (some with CTCSS) and one AM channel.

```bash
python -m sdrscanner.tools.gen_iq demo --seconds 60
```
```bash
python -m sdrscanner demo_profile.json --start
```

You can also play any recording with `python -m sdrscanner --iq capture.sigmf-meta`.

## Using it

1. **Radio settings**: choose the source (RTL-SDR, rtl_tcp or IQ file), sample
   rate, gain/AGC, and PPM correction.
2. **Add channels** in any of these ways:
   - **Add channel…** (Ctrl+N), or double-click the spectrum. Set the name,
     frequency, mode (FM/AM), bandwidth, squelch, hysteresis, hang time, tone
     squelch (CTCSS or DCS), de-emphasis, volume, pan, output device, priority,
     alert, recording, transmission hold and channel lists.
   - **Channels → Add from preset…** (Ctrl+P). See [Preset lists](#preset-lists).
   - **Channels → Import CSV…** (Ctrl+I). See
     [Importing channels from CSV](#importing-channels-from-csv).
3. Press **Start** (F5).

The status bar shows the chosen centre frequency, the usable span, and any
warning. You get a warning when channels don't fit in the capture bandwidth or
sit on top of the DC spike. The **Channels** menu can also enable or disable
everything, sort by frequency, set every squelch at once, remove all channels,
or export the list to CSV.

### Channel lists

A channel list is a named group of channels: "Public safety", "Marine",
"CB", "Ham repeaters" and so on. A channel can be in any number of lists. Pick
the list to receive with **Monitor:** in the toolbar, Ctrl+1 … Ctrl+9, or
Ctrl+` for all channels.

- Only the channels in that list are received and shown. The dongle retunes for
  them, so switching from a 156 MHz marine list to a 27 MHz CB list just works.
- Manage lists with **Channels → Manage channel lists…** (Ctrl+L): new, rename,
  duplicate, delete, and tick the channels that belong to each.
- Add channels to lists from a channel's right-click menu, the channel dialog,
  or the bulk editor.
- When adding a preset or importing a CSV you can put the new channels straight
  into a new list and start monitoring it.
- New channels added while a list is active join that list.
- Lists are saved in the profile and exported in the CSV `lists` column.

### Selecting, reordering and editing several channels

- **Select** channels by clicking a strip. Ctrl+click adds or removes one, and
  Shift+click selects a range. Ctrl+A selects all.
- **Reorder** by dragging strips (the whole selection moves), with Alt+Up /
  Alt+Down, or with the right-click menu (move up, down, to top, to bottom).
  Order is kept per profile and used by the timeline and CSV export. When a list
  is active, only its channels move; the others keep their places.
- **Edit several at once:** Ctrl+E (or right-click → *Edit N channels
  together…*) opens the bulk editor. Tick the settings to change, such as
  squelch (a fixed level or *noise floor + N dB*), tone squelch, bandwidth,
  mode, hold time, volume, pan, mute, record, priority, alert, output device or
  list membership. Only the ticked settings change, and only on the ticked
  channels. FM-only settings skip AM channels.
- Right-click also offers duplicate, delete, *set squelch from noise floor*,
  temporary mute, and *use detected tone/code as tone squelch*.

### Priority, alerts and temporary mute

- **Priority** (★): while a priority channel is active, every other channel is
  turned down. The level is set in *Radio settings → Priority and alerts*
  (default −15 dB; −60 dB is practically mute).
- **Alert** (⚑): when the channel becomes active you get a desktop notification
  and/or the system alert sound, at most once every 15 seconds per channel. The
  alert is also written to the Messages tab.
- **Temporary mute:** right-click → *Mute for* 5, 15 or 60 minutes. The M
  button shows `M⏱` until the mute ends by itself.
- **Last heard:** each strip shows when the channel last had activity and how
  many transmissions it has had this session. Hover for total airtime.

### Preset lists

| Preset | Channels | Mode | Fits in one capture? |
|---|---|---|---|
| FRS (US/CA) | 22 | NFM 12.5 kHz | 1–7 + 15–22 together; 8–14 are 5 MHz higher |
| GMRS (US) | 22 | FM 25 kHz (8–14 narrow) | same split as FRS |
| MURS (US) | 5 | NFM / FM | spans 2.8 MHz: pick 151 MHz or 154 MHz |
| CB (40 ch) | 40 | AM 8 kHz | yes (27 MHz; R820T/R828D tuners reach it) |
| NOAA Weather Radio | 7 | FM 25 kHz | yes |
| Marine VHF (US common) | 12 | FM 25 kHz | yes |
| 2 m amateur simplex (US) | 13 | FM 25 kHz | yes |
| PMR446 (Europe) | 16 | NFM 12.5 kHz | yes |
| Aviation (US common) | 11 | AM 8 kHz | yes; add your local tower/ATIS yourself |

The picker plans the centre frequency for your current channels plus the
selected ones, and marks each row *fits*, *outside capture band*, or *already in
your list*. **Select only those that fit** keeps the largest group that can be
received at once. Set the squelch for the new channels in the picker, then
fine-tune each one with its **Auto** button.

### Importing channels from CSV

The first row must be a header. Columns can be in any order, and only a
frequency column is required. The delimiter can be `,`, `;` or tab; decimal
commas (`462,5625`) work, as does UTF-8 or Windows-1252 text. Lines starting
with `#` are ignored.

```csv
name,frequency_mhz,mode,bandwidth_khz,squelch_db,ctcss_hz,volume_pct,pan_pct,enabled
Dispatch,460.1250,FM,12.5,-55,100.0,100,-50,yes
Tower,118.300,AM,8,-60,,120,50,yes
CB 19,27.185,AM,,,,,,yes
```

| Column (aliases) | Meaning |
|---|---|
| `frequency` / `frequency_mhz` / `freq` / `rx_frequency` | Required. Units can be in the header or the cell (`146.52`, `146.52 MHz`, `27185k`, `146520000 Hz`). Plain numbers ≤ 6000 are MHz. |
| `name` / `label` / `alpha_tag` | Defaults to the frequency |
| `mode` | `FM`/`NFM` or `AM`. WFM and digital modes are rejected. |
| `bandwidth` / `bandwidth_khz` / `bw` | Defaults: FM 12.5 kHz (`NFM` → 12.5 kHz), AM 8 kHz |
| `squelch_db`, `hysteresis_db`, `hang_time_s`, `hold_time_s` | dBFS, dB, seconds, seconds |
| `ctcss_hz` / `tone` / `pl` | Standard tone in Hz, or `off` (FM only) |
| `dcs` / `dtcs` / `dpl` | DCS code such as `023`, `D023N`, `754I` (FM only; wins over CTCSS) |
| `dcs_polarity` | `N`, `I` (or `R`) or `either`; overrides a suffix on the code |
| `priority`, `alert` | yes/no |
| `lists` | Channel lists to put the channel in, separated by `\|` (e.g. `Local\|Repeaters`) |
| `deemphasis_us` | `750` (default) or `0`/`off` |
| `volume_pct`, `pan_pct` | 0–200 %, −100 (left) … +100 (right) |
| `enabled`, `record` | yes/no, true/false, 1/0 |
| `audio_device`, `color` | Output device name as shown in the channel dialog; `#rrggbb` |

**CHIRP exports** are recognised automatically. For those, `Frequency` is the
receive frequency, `Mode` FM = 25 kHz and NFM = 12.5 kHz, and `Tone` = TSQL uses
`cToneFreq` as tone squelch. `Tone` = DTCS uses `DtcsCode` as DCS squelch, with
the receive polarity taken from the second letter of `DtcsPolarity` (R =
inverted). Cross mode uses the receive half of `CrossMode` (`…->Tone` or
`…->DTCS`, with `RxDtcsCode`). Plain `Tone` (transmit tone only) falls back to
carrier squelch, and `Skip` = S imports the channel disabled. Rows that can't
be used are listed with the reason and skipped. **File → Export channels to
CSV…** writes the same format, so lists round-trip and can be edited in a
spreadsheet.

### View options

| View menu | What it does |
|---|---|
| Theme | Dark or light |
| Layout | Spectrum above the channels, or side by side (good for wide screens) |
| Show spectrum / waterfall / channel panel / toolbar / status bar | Hide what you don't need (one panel always stays visible) |
| Streamlined view (Ctrl+M) | Hides the menu bar, toolbar and panel tabs for a clean second-monitor display |
| Compact channel strips | One line per channel, for long lists such as the 40 CB channels |
| Channel labels | Names on the spectrum for all channels, active channels only, or none |
| Waterfall colours | Inferno, magma, plasma, viridis, cividis, turbo, grayscale |
| Peak hold | Slowly decaying maximum trace on the spectrum |
| Zoom to channels (Ctrl+=) / Show full band (Ctrl+0) | The mouse wheel zooms and dragging pans too |
| Window mode | **Normal**, **Borderless** (Ctrl+B) or **Full screen** (F11; Esc leaves) |
| Always on top | Keep the scanner above other windows |
| Display settings… | FFT size (1024–8192), spectrum averaging, waterfall speed, automatic or fixed levels |

**Borderless window** removes the title bar and frame, which is handy for a
station display on a second monitor. The menu bar acts as the title bar: drag
its empty area to move the window, and double-click it to maximise. Minimise,
maximise and close buttons sit at its right end. Drag any window edge or corner
to resize.

**Streamlined view** (Ctrl+M, or right-click the spectrum) hides the menu bar,
toolbar and the tab bar above the channel panel. Combined with borderless or
full screen it leaves just the spectrum, waterfall and channels. Everything
stays reachable: Ctrl+M or right-click brings the bars back, and other
shortcuts keep working. A borderless window can be moved by **Alt+dragging**
anywhere in it.

View settings are saved per user (`view.json` next to the last profile),
separately from channel profiles, so sharing a profile doesn't change anyone's
layout.

### How the centre frequency is chosen

The usable part of the capture is about 85% of the sample rate (about
2.04 MHz at 2.4 MS/s). The tuner's anti-alias filter rolls off beyond that.
The dongle also has a spike of LO leakage exactly at the tuned frequency. With
*automatic centre* on (the default), the scanner picks the centre that:

1. keeps every enabled channel inside the usable span, and
2. leaves at least 15 kHz between the DC spike and any channel, and otherwise
   stays close to the middle of the channel group.

If the channels span more than the usable bandwidth, it covers the largest
group that fits, marks the rest as *out of band*, and shows a warning. When you
add, move or enable channels while running, the dongle is retuned
automatically. For IQ file playback, the centre is fixed by the recording.

### Squelch

- **Open level** (dBFS): the channel opens when its in-channel power reaches
  this level. Click **Auto** to set it 8 dB above the measured noise floor (the
  grey tick on the meter).
- **Hysteresis**: once open, the channel stays open until the signal drops this
  many dB below the open level. This stops it flapping on weak signals.
- **Hang time**: how long the channel stays open after the signal goes away.
  This bridges short fades and pauses in speech.
- **CTCSS** (FM only): when set, the channel only opens if that tone is present.
  The detected tone is shown on the strip for every FM channel either way, which
  helps you find out what tone a channel uses. The tone must be confirmed first,
  so a tone-squelched channel opens about 0.5 s after the carrier appears.
- **DCS** (FM only): when set, the channel only opens for that code. Every
  standard code is supported, and the detected code is shown on the strip
  (e.g. `♪ DCS 023N`) and logged. Polarity can be *Normal*, *Inverted* or
  *Either*. Several codes describe the same signal (023 = 340 = 766, and 023
  inverted = 047 normal), so either name works. Detection takes about 0.5 s
  after key-up.
- **Transmission hold** (default 2 s): after the squelch closes, the recording
  and the log entry stay open this long. If the signal returns, for example
  between the dots and dashes of a repeater's morse ID or after a short fade, it
  continues the same WAV file (the gap is written as silence) and the same log
  entry. Set it to 0 to split at every drop.

### Audio routing

Each channel picks an output device. Channels on the same device are mixed.
**Pan** fades the channel between left and right: at centre, both sides play at
full level. **Solo** on any channel mutes every channel that isn't soloed.

### Files

| What | Where / format |
|---|---|
| Profiles | Any `.json` (*File → Save profile*). The last session is saved to `%APPDATA%\SDRScanner\last_profile.json` or `~/.config/SDRScanner/` |
| View settings | `view.json` in the same folder |
| Channel lists | CSV (*File → Import / Export channels*) |
| Transmission recordings | `~/SDRScanner/recordings/` (configurable), `YYYYmmdd_HHMMSS_<channel>_<MHz>.wav` |
| IQ recordings | Same folder, SigMF `cu8` (`.sigmf-data` + `.sigmf-meta`), about 4.8 MB/s at 2.4 MS/s |
| Activity log | *File → Export activity log (CSV)* |

Supported IQ playback formats: SigMF (`cu8`, `ci8`, `ci16_le`, `cf32_le`); WAV IQ
from SDR#/SDR++/HDSDR (centre frequency read from the `..._146520000Hz_...`
file name); and raw `.cu8`/`.bin` (rtl_sdr), `.cs8`, `.cs16`, `.cf32`/`.cfile`.
Enter the centre frequency and sample rate in settings if the file name doesn't
contain them.

### rtl_tcp

Run `rtl_tcp -a 0.0.0.0 -s 2400000` on the machine with the dongle. Then choose
*rtl_tcp server* in Radio settings and enter its address. Gain, PPM and retuning
all work over the network. rtl_tcp carries 4.8 MB/s at 2.4 MS/s, so use a wired
link or a low sample rate. rtl_tcp has no authentication or encryption, so only
use it on a network you trust.

### Keyboard shortcuts

F5 start/stop · Ctrl+N add channel · Ctrl+P presets · Ctrl+I import CSV ·
Ctrl+O/Ctrl+S open/save profile · Ctrl+L channel lists · Ctrl+1…9 / Ctrl+` monitor a
list / all · Ctrl+A select all · Ctrl+E edit selected · Alt+Up/Down move · Del delete
selected · Ctrl+M streamlined view · Ctrl+B borderless · F11 full screen (Esc leaves) ·
Alt+drag move a borderless window · Ctrl+= zoom to channels · Ctrl+0 full band.
They're also listed under *Help → Keyboard shortcuts*.

---

## How it works

```
 reader thread                 DSP thread                                       audio callbacks
 ┌───────────┐  chunks   ┌────────────────────────────────────────────────┐   ┌──────────────┐
 │ RTL-SDR / ├──────────►│ FFT (shared) ─► all channels as matrix rows:   ├──►│ jitter buffer│─► device 1
 │ rtl_tcp / │  queue    │   bin select ×H ─► batched IFFT (= 48 kHz)     │   │ per device   │─► device 2
 │ IQ file   │           │   ─► FM discriminator / AM envelope + AGC      │   └──────────────┘
 └───────────┘           │   ─► CTCSS, squelch, de-emphasis, voice BPF    │
                         │   ─► volume/pan mix per device, WAV, log       │
                         └────────────────────────────────────────────────┘
                                        │ snapshots (25 Hz)
                                        ▼
                                  Qt GUI thread
```

- **Channelizer** (`sdrscanner/dsp.py`): an overlap-save fast-convolution
  filter bank. One FFT of the wideband signal (51 200 points at 2.4 MS/s, 75%
  hop) is shared by all channels. Each channel takes the bins around its
  frequency and applies its channel filter, and then one 1024-point inverse FFT
  per channel extracts, filters and decimates it straight to 48 kHz. A per-frame
  phase correction and a fine NCO make the result identical to a continuous
  mixer, so there are no clicks at block boundaries.
- **Channel bank**: every channel is a row of the same matrices (batched IFFT,
  discriminator, filters, CTCSS detector), so the per-frame cost barely depends
  on the channel count. In testing, all 40 CB channels used about 15% of one
  CPU core at 2.4 MS/s, or about 20% with the GUI running.
  Adding or removing a channel keeps every other channel's filter state, so
  nothing glitches.
- **FM**: quadrature discriminator, then 750 µs de-emphasis (switchable), then
  a 300–3000 Hz voice band-pass, which also removes the CTCSS tone.
- **AM**: envelope detector divided by a tracked carrier level. That AGC makes
  the output level depend only on modulation depth, not signal strength. Then
  the same voice band-pass.
- **CTCSS**: the discriminator output is low-passed and decimated to 3 kHz into
  a ring buffer. The 51 standard tones are measured over a 0.5 s window, and a
  tone is accepted only when it dominates the sub-audio band in several
  estimates in a row.
- **DCS** (`sdrscanner/dcs.py`): a code word is a 23-bit Golay (23,12) word: the
  9-bit code with the fixed bits `100`, plus 11 check bits from generator 0xC75
  (DCS 023 = 0x763813). It is sent continuously, least significant bit first,
  at 134.4 bit/s.
  - The detector slices the last 0.75 s of the sub-audio stream into bits at six
    clock phases and looks up every 23-bit window in a sorted table of all 2392
    rotations of the 104 standard codes. This is vectorised across channels.
  - A clean stream matches on every window, so a code is reported after at
    least 23 agreeing windows in two consecutive checks. Noise matches about
    0.03% of windows, so false codes are effectively impossible.
  - Polarity convention: *normal* means a 1 bit raises the carrier frequency.
- **Threads**: the USB/network/file reader, the DSP, and each PortAudio
  callback run off the GUI thread. The GUI polls a snapshot 25 times a second
  and never blocks. If the DSP falls behind, the oldest chunks are dropped and
  counted in the status bar.
- **Disconnects**: if the dongle is unplugged or the rtl_tcp connection drops,
  the status changes to "waiting for source". The reader retries every 2 s and
  carries on when the device is back.

## Security notes

Profiles, channel CSVs, IQ recordings and rtl_tcp streams may come from other
people, so they are treated as untrusted input:

- **Size limits.** Profiles are capped at 5 MB, CSVs at 2 MB / 2000 rows, SigMF
  metadata at 1 MB, and profiles at 256 channels.
- **Validation.** Every number must be finite and is clamped to a sane range.
  Absurd IQ sample rates are refused, so a crafted file can't trigger a huge
  allocation.
- **Text.** Names and other strings (channel names, list names) are
  length-limited, and control and bidi characters are stripped. DCS codes must
  be valid octal codes. Lists may only reference existing channels, and there
  are at most 64 lists.
- **Malformed files** produce an error message, never a crash. Drag-and-drop
  data is validated against the channels on screen.
- **No HTML.** User-supplied text (channel names, device names, file names) is
  never rendered as HTML in the interface.
- **Exported CSVs** neutralise spreadsheet formula injection: cells starting
  with `=`, `+`, `-`, `@` and similar get a leading `'`.
- **Recordings.** File names are reduced to a safe fragment (no path
  separators), so a channel name can't write outside the recordings folder.
  Existing files are never overwritten.
- **DLLs.** A user-supplied `librtlsdr` is only loaded from the application's
  own folder, never the current directory.
- **No code execution.** The program never runs shell commands and never
  evaluates or unpickles data.
- **CI.** The release workflow uses only GitHub's own actions and the `gh` CLI.
  The release tag is validated and passed through environment variables, never
  inlined into scripts.

Still worth knowing: rtl_tcp is an unauthenticated plain-TCP protocol. A shared
profile can set a recordings folder, and recordings (`.wav`/`.sigmf-*` files)
are then written there.

## Development

```bash
pip install -r requirements-dev.txt
```
```bash
python -m pytest
```

The suite has 220 tests:

- **DSP**: tone recovery at every supported sample rate, FM and AM including
  AGC, mixed multi-channel banks, seamless bank rebuilds, adjacent-channel
  rejection, squelch and CTCSS.
- **DCS**: the published 023 codeword, table structure, documented inversion
  and rotation aliases, parsing, decoding in noise, no false codes, and tone
  squelch with right, wrong and either polarity.
- **Planner and config validation**, including fuzzed and hostile profiles.
- **CSV import/export**, including CHIRP files and a fuzzer.
- **Presets**.
- **Every IQ file format**, including a corrupt-WAV fuzzer.
- **rtl_tcp** against a fake server, including reconnects.
- **Engine behaviour** under live channel edits and retuning, audio routing
  (pan, mute, solo), priority ducking, temporary mutes, channel lists,
  transmission hold (merging and splitting), DCS end to end, IQ record and
  playback, and restarts.
- **Security regressions**.
- **Headless GUI tests**: window modes, the streamlined view and Alt+drag, view
  options, picker, selection, drag reordering (including within a list), bulk
  edit, channel lists, alerts, learning tones, last heard, playing recordings
  from the log, and HTML escaping.

On a headless Linux box, run with `QT_QPA_PLATFORM=offscreen`.

### Building the Windows .exe

From PowerShell in the project folder:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

The script:
1. creates a clean `.venv-build` environment and installs the dependencies
   plus PyInstaller;
2. builds `dist\SDRScanner\SDRScanner.exe` from `packaging\sdrscanner.spec`;
3. runs the exe's self-test;
4. zips the folder to `dist\SDRScanner-<version>-win64.zip`.

Add `-OneFile` for a single `SDRScanner.exe`. It starts slower because it
unpacks itself on every launch, and antivirus flags it more often.

The bundle includes Python, Qt, NumPy/SciPy, PortAudio (from `sounddevice`) and
librtlsdr (from `pyrtlsdrlib`, with libusb statically linked). Users only need
the Zadig driver.

The *Windows build* GitHub Actions workflow (`.github/workflows/windows-build.yml`)
does the same on every push and pull request. It runs the test suite on
Windows, builds the exe, runs its self-test, and uploads the zip as an
artifact.

To publish a GitHub release with the zip, either push a tag such as `v0.1.0`,
or run the workflow manually on `main` (*Actions → Windows build → Run
workflow*) with *release_tag* set. A manual run creates the tag for you. In both
cases the tag must match the app version. When you bump the version, update
`sdrscanner/__init__.py`, `pyproject.toml` and `packaging/version_info.txt`
(a test checks they agree).

`--self-test` checks the FM and AM DSP chain, PortAudio, librtlsdr loading, and
the GUI with an IQ playback run. No dongle or sound card is needed. It exits
non-zero on failure, and because the exe has no console window, it writes its
report to the file you name.

Package layout:

```
sdrscanner/
  config.py      data model, validation, JSON profiles
  planner.py     centre-frequency selection, DC-spike avoidance, band warnings
  dsp.py         channelizer, channel bank (FM/AM demod, CTCSS, squelch), spectrum
  sources.py     RTL-SDR, rtl_tcp, IQ file sources
  audio.py       per-device output streams, jitter buffer, pan law
  activity.py    activity events, CSV export, WAV + IQ recorders
  channel_io.py  channel CSV import (native + CHIRP) and export
  presets.py     built-in channel lists
  engine.py      reader/DSP threads, retuning, mixing, GUI snapshots
  native.py      librtlsdr DLL discovery (user-supplied DLL overrides the bundled one)
  selftest.py    --self-test installation check
  gui/           PySide6 + pyqtgraph interface (main window, spectrum, picker, themes, view settings)
  tools/gen_iq.py  synthetic multi-channel FM/AM test-signal generator
packaging/       PyInstaller spec, launcher, icon, Windows build script
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| "librtlsdr could not be loaded" | Install `pyrtlsdr[lib]`, or put `librtlsdr.dll`/`rtlsdr.dll` + `libusb-1.0.dll` next to `SDRScanner.exe` / `run_scanner.bat` (Windows). On Linux install `librtlsdr0`. Run `--self-test` to see which library is picked up. |
| "Cannot open RTL-SDR #0" (retrying) | Windows: redo the Zadig step for Interface 0. Linux: blacklist `dvb_usb_rtl28xxu` and check udev permissions. Close other SDR programs, since only one can use the dongle. |
| Channels are a few kHz off | Set *Frequency correction (ppm)*. Tune a known signal and adjust until it sits centred in its marker. |
| Preset channels show "out of band" | The list spans more than one capture (FRS, GMRS, MURS). In the picker, use **Select only those that fit**, or raise the sample rate. |
| Nothing on CB / 27 MHz | Your tuner must reach 27 MHz (R820T/R828D do; E4000 starts at about 52 MHz). Use a suitable HF/CB antenna. |
| Choppy audio, "dropped" counter rising | Lower the sample rate, use fewer channels, or close CPU-heavy programs. Check the *DSP load* figure in the status bar. |
| Image of a strong signal mirrored around the centre | This is RTL-SDR IQ imbalance. Lower the gain, or move the channel by changing the manual centre. |
| Borderless window is hard to grab | Drag the empty part of the menu bar, Alt+drag anywhere, or press Ctrl+B to get the normal frame back. |
| The menus have disappeared | Streamlined view is on: press Ctrl+M or right-click the spectrum. |
| A known DCS channel never opens | Set its polarity to *Either polarity*. Some radios invert the code. Also check the strip's detected code (`♪ DCS …`). |
| One transmission makes several recordings | Raise the channel's *Transmission hold* (bulk editor → *Transmission hold* for many channels at once). |
| "Audio disabled: PortAudio library not found" (Linux) | `sudo apt install libportaudio2` |
