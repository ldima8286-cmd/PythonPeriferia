# Periferia

Global push-to-talk on any key, virtual microphone, and microphone processing
for Linux. Python, PipeWire, evdev. Works on Wayland.

The microphone is closed unless you are holding a key you chose. Not a toggle:
release the key and the mic is muted again. This works for Discord, Zoom, OBS,
a browser, or anything else, because Periferia controls the sound server rather
than any one application.

## What exists right now

- `audio` — a virtual microphone created from the physical one, with a smooth
  volume gate. This is the part PTT is built on.
- `ptt` — reads keys over evdev, opens the mic on press, closes it on release,
  with a hold time so the last word is not clipped, and a panic key.
- `processing` — turns on PipeWire's own RNNoise noise suppression, echo
  cancellation and voice detection. These are module properties, not DSP
  written here.
- `cli` — environment check, device listing, key picker, manual volume ramp.

Not built yet: tray icon, input profiles, RGB, GUI. See "Roadmap".

## Install

```bash
git clone https://github.com/ldima8286-cmd/PythonPeriferia
cd PythonPeriferia
bash scripts/dev-setup.sh
source .venv/bin/activate
```

Needs Python 3.11+ and `pipewire-utils`. On Arch the audio packages are usually
present already; elsewhere: `sudo apt install pipewire wireplumber pipewire-utils`.

## First run

```bash
periferia check        # is this machine able to run it
periferia sources      # what capture devices exist
periferia pick-key     # press a key, get the config value for it
periferia init-config  # write ~/.config/periferia/config.yaml
```

Put the key you got from `pick-key` into `ptt_key`, then point your apps at the
virtual mic:

```bash
periferia set-default
```

`periferia check` is worth reading carefully. It tells you whether you can read
key events, whether the sound server is reachable, and whether a Flatpak app
will be able to see the audio. If key events are not readable, see
[docs/udev.md](docs/udev.md).

## Usage

```bash
periferia daemon        # or: python -m periferia.core.daemon
```

Hold the key: mic opens. Release: mic closes after `hold_ms`.

| command | what it does |
| --- | --- |
| `periferia check` | verify the environment, prints what is missing |
| `periferia sources` | list sources, marks the physical one |
| `periferia devices` | list input devices that look like keyboards |
| `periferia pick-key` | press a key, get the yaml value for it |
| `periferia ramp` | manual volume ramp, to check for clicks before PTT works |
| `periferia set-default` | point the default source at the virtual mic |
| `periferia teardown` | unload loopback modules left behind by a crash |
| `periferia config` | show the config actually in effect |
| `periferia install-service` | install the systemd user unit |

## Configuration

`~/.config/periferia/config.yaml`, or `$PERIFERIA_CONFIG`. See
`config.example.yaml`. The values that matter most:

| key | default | notes |
| --- | --- | --- |
| `audio.attack_ms` | 10 | shorter feels more instant |
| `audio.release_ms` | 60 | how fast the mic fades out |
| `audio.hold_ms` | 200 | keeps the mic open briefly after release |
| `audio.curve` | exp | `exp`, `linear` or `s_curve` |
| `ptt.ptt_key` | auto | physical key, see below |
| `ptt.panic_key` | KEY_F12 | instant mute, ignores `hold_ms` |

## About the key names

evdev reports physical key codes and knows nothing about keyboard layout.
`KEY_V` is the physical V key, which prints `M` on a Russian layout. The config
takes the physical key, so the same key works in both layouts, and `pick-key`
prints both the code and the letter for reference.

`KEY_W KEY_A KEY_S KEY_D` is movement in most games, so those are poor PTT
choices. `KEY_Y`, `KEY_U`, `KEY_H`, `KEY_N` and `KEY_GRAVE` are rarely used
(they are Н, Г, Р, Т, Ё on a Russian layout).

## Why the virtual microphone

The physical mic is never muted. A `module-loopback` virtual source is created
from it, and applications are pointed at the virtual one. PTT moves only the
virtual source's volume. Two reasons: the physical device stays available to
anything else, and a smooth ramp can be applied, which is not possible when a
device is muted outright, because that clicks.

Volume is ramped rather than switched, over `attack_ms`, so there is no click
in the headphones.

## Why no DSP here

Noise suppression, echo cancellation and voice activity detection are all
already in PipeWire, as properties of `module-echo-cancel`. Periferia sets
those properties. Compressor and de-esser are not built in and are the only
things that might eventually need real code, probably via LADSPAF.

## Latency

The floor is the PipeWire quantum, not this program. The default 1024 at
48 kHz is about 21 ms per buffer. Lowering it to 256 (about 5 ms) is a
`pipewire.conf` setting and helps more than anything in this repository. Going
too low makes the whole chain glitch if heavy filters are in use.

## Troubleshooting

**Keys are not detected.** `periferia check` marks unreadable devices. See
docs/udev.md.

**Mic opens but nothing is heard.** The app is probably still on the physical
source. Run `periferia sources`, then `periferia set-default`.

**Clicks on open or close.** Raise `attack_ms` and `release_ms`.

**Keys type into the app anyway.** The device is not grabbed. This happens when
another program grabs the same device first. Check with
`sudo fuser -v /dev/input/event5`.

**Left-over module after a crash.** `periferia teardown`.

**Flatpak app sees no sound.** Flatpak needs a Pulse socket. `periferia check`
reports on this.

## Roadmap

- [ ] tray icon and overlay indicator
- [ ] GUI for the config
- [ ] input profiles: remap, DPI, disable keys, per-window switching
- [ ] RGB control with scripts and time-of-day profiles
- [ ] compressor and de-esser via LADSPAF
- [ ] direct PipeWire graph control, instead of going through pactl
- [ ] Flatpak packaging, AUR

Window tracking on Wayland is the hard part of input profiles: compositors do
not hand that out freely. Worth checking before investing in that module.

## License

MIT.
