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
  with a hold time so the last word is not clipped, and a panic key. Watches
  every keyboard at once, not just the first one found.
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

Needs Python 3.11+ and `pipewire-utils`. On Debian, Ubuntu, Arch, openSUSE and
Alpine the script prints the exact package command if `pactl` is missing.

### On Fedora Atomic, Bazzite and other immutable systems

`dnf` does not install software there, `rpm-ostree` does it by changing the
whole system and needs a reboot. Neither is needed here.

`evdev` is a C extension, so pip tries to compile it and needs `Python.h`.
On an immutable system that header is not in the base image. The fix is `uv`,
which brings its own interpreter, headers included, so nothing has to be
compiled:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
rm -rf .venv
bash scripts/dev-setup.sh
source ~/.bashrc   # so uv is on PATH in new shells
```

The script uses `uv` when it finds it and targets Python 3.13, for which `evdev`
ships manylinux wheels. On an immutable system without `uv` it stops and says
so, rather than falling back to the system Python and failing later with a
compiler error.

`pipewire-utils` is part of the Bazzite base image, so `pactl` should be there.
If `periferia check` says it is not, use a toolbox or a container.

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
periferia-daemon          # installed by scripts/dev-setup.sh
```

Hold the key: mic opens. Release: mic closes after `hold_ms`. The daemon only
reads the keyboard, it never grabs it, so your typing keeps working normally.

| command | what it does |
| --- | --- |
| `periferia check` | verify the environment, prints what is missing |
| `periferia sources` | list sources, marks the physical one |
| `periferia devices` | list input devices that look like keyboards |
| `periferia pick-key` | press a key, get the yaml value for it |
| `periferia ramp` | manual volume ramp, to check for clicks before PTT works |
| `periferia set-default` | point the default source at the virtual mic |
| `periferia teardown` | unload echo-cancel modules left behind by a crash |
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
| `ptt.device` | auto | which keyboards to watch, see below |
| `ptt.panic_key` | KEY_F12 | instant mute, ignores `hold_ms` |

## Keyboards and other input devices

By default Periferia watches **every** keyboard it can open, not just the first
one. A laptop keyboard, an external USB keyboard and a Bluetooth keyboard are
three devices, and the same key must work on all of them. It identifies them by
capability rather than by name, because the name in `/dev/input/by-id` is
whatever the vendor typed in and a mouse also exposes a keyboard-style
interface.

Press state is shared between them. Holding PTT on the laptop keyboard and
releasing it on the external one still closes the microphone, instead of
leaving it stuck open. Unplugging a keyboard mid-hold drops that device and
keeps working on the rest.

To restrict it, set `ptt.device` to one node or to several, comma separated:

```yaml
ptt:
  device: /dev/input/by-id/usb-SomeVendor_Keyboard-event-kbd
  # or several, if you only trust some of them:
  # device: /dev/input/event4, /dev/input/event7
```

`periferia pick-key` listens on all keyboards as well, so the key can be
pressed on whichever one you like.

## About the key names

evdev reports physical key codes and knows nothing about keyboard layout.
`KEY_V` is the physical V key, which prints `M` on a Russian layout. The config
takes the physical key, so the same key works in both layouts, and `pick-key`
prints both the code and the letter for reference.

`KEY_W KEY_A KEY_S KEY_D` is movement in most games, so those are poor PTT
choices. `KEY_Y`, `KEY_U`, `KEY_H`, `KEY_N` and `KEY_GRAVE` are rarely used
(they are Н, Г, Р, Т, Ё on a Russian layout).

## Why the virtual microphone

The physical mic is never muted. `module-echo-cancel` is fed from it and
publishes a separate virtual source under the name applications look for, and
PTT moves only that virtual source's volume. Two reasons: the physical device stays available to
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

## What is tested where

Being honest about this, because it decides how you should file a bug.

| | status |
| --- | --- |
| Fedora, Wayland, PipeWire 1.6, two keyboards | tested, PTT verified end to end |
| Fedora Atomic / Bazzite | supported by the setup script, not verified |
| Debian, Ubuntu, Arch, openSUSE, Alpine | no automated testing, dependency names only |
| PulseAudio instead of PipeWire | not supported, `module-echo-cancel` is PipeWire |
| X11 | should work, not verified |
| Keyboards over Bluetooth | should work if the udev rule matches them, not verified |

There are no CI runs, so "works on my machine" is currently the only evidence.
If you get it running somewhere else, the useful thing to report is the distro,
the desktop session, and the output of `periferia check`.

## Troubleshooting

**Keys are not detected.** `periferia check` marks unreadable devices. See
docs/udev.md.

**A key works on one keyboard but not another.** The other one is probably
blocked by permissions. `periferia check` lists which nodes it could not open.

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
