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
- `macros` — records keypresses with their timing and replays them from one key,
  globally or overridden per window. Chords are recorded as they were typed, with
  several keys held at once. Purely a keyboard feature; the microphone is not
  involved.
- `cli` — environment check, device listing, key picker, manual volume ramp,
  and a `status` that reports whether the microphone is open.
- `gui` — a window with four pages: state, profiles, keys, checks.

The daemon watches the config file, so a finished macro recording or an edited
keyboard profile is picked up within about a second. See "Macros".

  Not built yet: RGB. The input profiles are configurable and validated, and on
  KWin the profile follows the focused window. See "Roadmap".

### Knowing whether the microphone is open

`periferia status` prints the current state, the source and the daemon pid:

```
$ periferia status
microphone OPEN, 0.4s ago
source        echo-cancel-source
daemon pid    1234
```

The daemon writes this to `$XDG_RUNTIME_DIR/periferia/state.json` whenever the
microphone changes. It is runtime state, not a setting, so it lives in the
runtime directory and is meaningless the moment the daemon is gone.

The tray reads that same file, which is why it needs no connection to the
daemon and shows nothing at all when the daemon is not running. A dot in the
corner for one number is a lot of Qt to ship, but the alternative is a keyboard
shortcut nobody remembers; both are one process reading a file.

## Install

### As one file

Download `Periferia-<version>-x86_64.AppImage`, then:

```bash
chmod +x Periferia-*.AppImage
./Periferia-*.AppImage
```

Nothing is installed. Put it anywhere and run it. To build it yourself:

```bash
bash scripts/build-appimage.sh
```

That needs network access, because it downloads PyInstaller and appimagetool,
and it downloads nothing else: the image is built from the commit you are on.
It refuses to run on a dirty tree, and it refuses to package a frozen build
that does not start.

To have it in the launcher, copy it to `~/Applications` (GNOME and KDE both
read that) and it appears on its own from the desktop entry inside.

### From source

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

## Checking that it works

`periferia tui` opens a self check in the terminal. Eight checks, each ending in
a pass or a fail rather than a wall of log output:

| key | check |
| --- | --- |
| 1 | environment, binaries, PipeWire, the graph, Flatpak audio |
| 2 | input devices, which are readable, which the daemon would watch |
| 3 | the virtual microphone exists and is named for applications |
| 4 | the processing properties really reached the module |
| 5 | **press and hold the PTT key**, the gate opens and closes |
| 6 | **panic** closes it instantly, ignoring `hold_ms` |
| 7 | the three ramp curves, timed |
| 8 | the configuration actually in effect |

Checks 5 and 6 need you: hold the key, then press the panic key. The rest are
automatic, and `a` runs
everything that needs no input.

It takes over the microphone while it runs, so stop the service first:

```bash
systemctl --user stop periferia
periferia tui
```

It needs no dependencies beyond the standard library, so it works over ssh and
in a container where a GUI would not start.

### When the microphone itself cannot be tested

Some of the chain can be proved without a working capture device, and it is
worth separating the parts, because "PTT does nothing" otherwise has three
possible causes and no way to tell them apart. The gate moves a source's
volume, and a source's volume moves whether or not any sound is passing
through it:

```bash
periferia gate-check
```

It reports the volume, waits half a minute, and says whether it rose while you
held the PTT key. If it rose, then the key reached the daemon, the ramp ran and
pactl accepted the write. What that does not prove is that any application
opens the source: that needs a recording, or a level meter in a voice chat.

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

Hold the key: mic opens. Release: mic closes after `hold_ms`. Hold it longer
than `latch_ms` and the mic latches open instead, closed by the next press, for
when you need both hands free. Latching is off by default; `periferia status`
shows the state, because a latch you cannot hear is a latch you forget you
turned on. The daemon only
reads the keyboard, it never grabs it, so your typing keeps working normally.

| command | what it does |
| --- | --- |
| `periferia check` | verify the environment, prints what is missing |
| `periferia sources` | list sources, marks the physical one |
| `periferia graph` | show the PipeWire graph the way cleanup reads it |
| `periferia devices` | list input devices that look like keyboards |
| `periferia pick-key` | press a key, get the yaml value for it |
| `periferia ramp` | manual volume ramp, to check for clicks before PTT works |
| `periferia set-default` | point the default source at the virtual mic |
| `periferia teardown` | unload echo-cancel modules left behind by a crash |
| `periferia status` | say whether the microphone is live right now |
| `periferia config` | show the config actually in effect |
| `periferia install-service` | install the systemd user unit |
| `periferia macro list` | every macro, what plays it, and what is in it |
| `periferia macro check` | what will not work, and why |
| `periferia macro play NAME` | play one macro once, without binding it |
| `periferia macro record NAME` | record a macro; `--bind`, `--stop-key`, `--in-profile` |
| `periferia macro delete NAME` | remove a macro |

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
| `ptt.latch_ms` | 0 | hold this long to latch the mic open, 0 disables |
| `ptt.max_press_ms` | 300000 | cut off a key held longer than this, 0 disables |
| `processing.voice_detect` | true | cuts silence between phrases, turn it off if quiet words get lost |
| `processing.stereo_to_mono` | true | publish one mono channel (left) instead of the untamed stereo source |
| `macros` | empty | recorded keypress sequences, see below |

## The window

Four things, in the order they get asked for:

| Page | The question it answers |
| --- | --- |
| Состояние | Is it working right now, and what is in the way |
| Профили | What does this do to my keys in which window |
| Правка профиля | Change one profile: which window it applies to, its keys, its pointer speed |
| Проверка | What is wrong with what I wrote |

The last page is the same checks `periferia profiles` runs, so a config can be
inspected without starting anything.

One profile is edited at a time. Saving writes only that profile's `name`,
`match`, `remap` and `pointer`, and leaves the rest of the file alone: comments,
section order, unknown keys, and any macros inside the profile are all kept as
they were. Macros are shown as a count and not edited here, because a recorded
sequence has its own timing that a table of key presses cannot ask for.

Every profile is editable, not just the first one, so a profile that names a
window can be changed without hand-editing the file. Choosing another profile in
the list drops whatever was unsaved in the one you were in, which is why saving
is an explicit button.

A profile's `match` is entered as plain text: `Класс окна` takes what the program
calls itself (`steam`), `Имя окна` the process name, `Заголовок` the window
title. Tick **применять везде** to make the profile the fallback one, which greys
the three fields out because a fallback has no conditions to match.

**Скорость указателя** is KWin's pointer speed, from 0.05 to 10, where 1.0 is
the desktop's own. Tick **менять** to put a speed in this profile; a profile
without that tick has no `pointer` section and never touches your settings.

## Which profile is in force

A profile with a `match` applies itself to the window you are in:

```yaml
profiles:
  - name: default          # no match, so this one is the fallback
    remap: { KEY_CAPSLOCK: KEY_ESC }
  - name: game
    match: { resource_class: [steam, lutris] }
    remap: { KEY_CAPSLOCK: KEY_TAB }
```

`match` takes `resource_class`, `resource_name` and `caption`, one value or a
list of them, and every field written has to match. The first profile that
matches wins, so put the specific ones first and leave one without a `match` at
the end as the fallback.

### Pointer speed

`pointer.speed` scales the mouse pointer while a profile applies. It is not
DPI: DPI belongs to the mouse, and software can only scale the pointer the
desktop draws.

```yaml
  - name: game
    match: { resource_class: steam }
    pointer: { speed: 2.5 }
    remap: { KEY_CAPSLOCK: KEY_TAB }
```

`1.0` is the speed the mouse was built with, and the range is `0.05` to `10`.
`periferia profiles` prints it.

The pointer is not the keyboard, so this does not go through `/dev/uinput`: it
goes into the compositor's own `kwinrc`, and KWin is asked to reread it. Only
KWin can be asked that way. Without `kde-config-tools` installed, the profile
still applies to the keys and the log says the pointer was left alone.

The speed the session had before is put back when a profile without a
`pointer` applies, and again when the daemon exits. A config that never mentions
`pointer` does not touch the file at all.

### Turning a key off

`none` as a target means the key does not exist while the profile applies:

```yaml
  - name: game
    remap:
      KEY_CAPSLOCK: none      # gone entirely
      KEY_F1: KEY_F13         # and this one moved
```

A key turned off this way produces nothing at all: the desktop does not see it,
a macro cannot play it, and it cannot open the microphone either. The key comes
back when you leave the window.

`periferia profiles` names the keys a profile turns off, because a key that has
gone missing is otherwise invisible, and it refuses `KEY_ESC`-style rescues in a
profile that only applies to some windows: if that window ever fails to be
detected, the way out stays gone. Held modifiers can be turned off even though
they cannot be remapped, since a key that emits nothing cannot get stuck.

Switching applies immediately. Rebuilding a remap table takes the keyboard for
as long as it takes, so it is done once per switch rather than once per event,
and only when the profile actually changes.

On **KWin** the window comes from a small script loaded into the compositor over
the session bus. Nothing else is supported: GNOME, X11 and other Wayland
compositors do not expose the focused window without a portal that does not
cover this, so there the first enabled profile stays in force and the log says
so. `periferia window` prints what the watcher currently sees, which is the
first thing to check when a profile does not apply.

Adding or removing the last `match` starts or stops the watcher on the next
config save. A config with no `match` at all loads no script into the session
and behaves exactly as before.

## The indicator

One dot, three colours, no configuration. It reads the state file, so it can be
run at any time and does not care whether the daemon is running:

```
periferia-tray              # tray icon, menu on click
periferia-tray --overlay    # the same dot painted on the desktop instead
periferia-tray --window     # the Qt window, for a session with no tray
```

Green means the microphone is open, grey means closed, amber means it is blocked
by a device still in use by something else. Nothing is shown when the daemon is
not running, which is the case that matters: an indicator that looks fine while
the daemon is dead is worse than no indicator.

`--overlay` draws it on the desktop, near the top right, click-through, and it
does not appear in the taskbar. That is for tiling setups where a tray exists
but nothing ever looks at it.

## Macros

A macro records keypresses with their timing and plays them back when you press
one key. It is only about the keyboard: nothing here touches the microphone.

```console
$ periferia macro record hello --bind KEY_F5
recording hello — press keys, F12 to save, Esc to cancel
hello: 3 keys, 0.4s
saved

$ periferia macro list
macros
  hello             KEY_F5  3 keys, 0.4s, [KEY_H KEY_E KEY_L]
```

| Command | What it does |
| --- | --- |
| `periferia macro list` | every macro, what plays it, and what is in it |
| `periferia macro check` | what will not work, and why |
| `periferia macro play NAME` | play it once, without binding it |
| `periferia macro record NAME` | record; `--bind KEY_F5`, `--stop-key KEY_F12`, `--in-profile game` |
| `periferia macro delete NAME` | remove it |

Recording captures how long each key was held and when it went down, so the
playback types at the speed you typed rather than as fast as the program can.

Several keys at once are recorded as they were, so a chord stays a chord:

```yaml
macros:
  - name: ctrl-c
    bind: KEY_F7
    steps:
      - key: KEY_LEFTCTRL
        at_ms: 0        # goes down first
        hold_ms: 40
      - key: KEY_C
        at_ms: 40       # while the modifier is still held
        hold_ms: 20
      - key: KEY_C
        at_ms: 60       # and comes back up before the modifier
        hold_ms: 20
      - key: KEY_LEFTCTRL
        at_ms: 80
        hold_ms: 40
```

`at_ms` is counted from the start of the macro, not from the previous key, which
is what lets two keys overlap. A step with a negative or missing `at_ms` plays
one after the one before it, so a plain list of single keys still works.

`gap_ms` is the older spelling: a pause measured from when the previous key came
up. It is still read, so macros recorded before chords existed keep playing as
they did, but it cannot express a chord and nothing new writes it.

If the program is stopped in the middle of a macro, every key it was holding is
released. Otherwise Ctrl would stay down for the rest of the session.

Two rules worth knowing:

- PTT and the panic key cannot be used. A macro on the panic key would fire
  while the panic was trying to stop it, and one that replays PTT would open the
  microphone every time it played. `macro check` says so rather than letting it
  surprise you.
- A macro can be defined globally and then overridden inside a profile, which is
  how one key means one thing everywhere and something else in one window:

```yaml
macros:
  - name: hello
    bind: KEY_F5
    steps:
      - key: KEY_H
        at_ms: 0
        hold_ms: 40
profiles:
  - name: game
    match: { resource_class: steam }
    macros:
      - name: hello        # same name, so this one wins in this window
        bind: KEY_F6
        steps:
          - key: KEY_GRAVE
            at_ms: 0
            hold_ms: 40
```

The profile has to already exist; `--in-profile` does not create one. If the
name does not match anything, the macro is saved globally instead and says so.

Preserving the same name across both places is deliberate. It means the profile
is an override of one macro rather than a second macro that happens to look
similar, and `macro check` can tell you which window a key will do what in.

### Saving and reloading

`periferia macro record` writes the config file while the daemon is running, and
the daemon notices within about a second. A recording is playable as soon as it
says `saved`; there is nothing to restart.

The same is true of the keyboard profile. Edit `profiles:` and save, and the
keyboard is picked up again a second later. Adding the first `match` starts
following the focused window on that same save, and removing the last one stops
it.

Neither reloads the microphone, the virtual source or the audio processing. Those
own a PipeWire node that your applications are pointed at, and rebuilding it would
drop every stream pointed at it. If the new config cannot be read, the daemon
says so and keeps running on the one it already had — a typo in one key name does
not cost you the microphone.

## Safety

The microphone is the one thing in this program that can embarrass you, so a
guard exists besides the key itself.

`max_press_ms` cuts off a key held longer than five minutes. The keyboard is
not grabbed, so events can be lost, and a press without its release would
otherwise open the microphone for good.

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

One physical keyboard usually appears as several nodes, for example
`usb-Vendor_Keyboard-event-kbd` and `usb-Vendor_Keyboard-if02-event-kbd`. Both
report the same presses, so those are collapsed into one device. Otherwise a
single keypress would arrive twice. `periferia devices` shows the raw list if
you want to see what was found.

To restrict it, set `ptt.device` to one node or to several, comma separated:

```yaml
ptt:
  device: /dev/input/by-id/usb-SomeVendor_Keyboard-event-kbd
  # or several, if you only trust some of them:
  # device: /dev/input/event4, /dev/input/event7
```

`periferia pick-key` listens on all keyboards as well, so the key can be
pressed on whichever one you like.

### Plugged in while it is running

A keyboard connected after startup is picked up on its own, within about a second
of settling down. There is nothing to restart, and the daemon looks again only
when the keyboard has been idle, so it costs nothing while you are typing.

### Which keyboard comes first

Every usable keyboard is watched no matter which one was used last. Priority is
only about the order they are opened and listed in, which matters because
`/dev/input/event4` is not the same node after a reboot.

To remember that order, the daemon writes `${XDG_RUNTIME_DIR}/periferia/keyboards.json`
after each scan. It is a cache, not configuration: deleting it costs nothing but
the remembered order, and it goes away with the session.

A keyboard is recognized by its physical path (`phys`, what the kernel reports for
where it is plugged in) and, for anything the kernel does not give a path to,
by its `/dev/input/by-id` name. Both survive a reboot, which an event node does
not. A keyboard that has neither is remembered by node number, which is the one
case that cannot be fixed from here.

A keyboard that is unplugged stops being remembered, so the next one to arrive is
not mistaken for it.

A mouse button works as the PTT key too, e.g. `BTN_SIDE` for a side button on a
gaming mouse. A mouse is only watched when the configured key is one of its
buttons, since reading one needs an extra udev rule that should not be granted
by default. To add it:

```bash
bash scripts/gen-udev-rules.sh "E-Signal"
sudo bash scripts/gen-udev-rules.sh "E-Signal" --install
sudo udevadm control --reload-rules
```

then set `ptt_key: BTN_SIDE` and restart.

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

**`periferia check` warns about the graph.** `pw-dump` is missing or the daemon
could not be reached. Cleanup falls back to `pactl`, which works but finds the
leftover by description instead of reading the id off the node. `pw-dump`
ships with `pipewire`; `periferia graph` prints what it can see.

**Flatpak app sees no sound.** Flatpak needs a Pulse socket. `periferia check`
reports on this.

## Roadmap

- [x] stereo to mono: the two channels really differ (measured), so the
      virtual microphone publishes one mono channel carried by the left channel
- [x] GUI for the config: profiles only, one at a time, comments kept
- [x] input profiles: remap, turn a key off entirely, pointer speed per
      profile, and on KWin the profile follows the focused window
- [ ] RGB control with scripts and time-of-day profiles. Needs raw HID access,
      which this machine does not expose
- [ ] compressor and de-esser. Not going to happen here: filter-chain will not
      load, so there is nowhere to put one. EasyEffects does this properly
      already
- [x] PipeWire graph read directly through `pw-dump`, so a leftover module is
      found off the node itself rather than guessed at through pactl. Writing
      still goes through pactl, which is the supported way to load a module
- [ ] Flatpak packaging, AUR

Remapping needs `/dev/uinput`, which this machine does not have, so the router
has never run against real hardware. The daemon falls back to reading the
keyboard without remapping and says so in the log, rather than refusing to
start: push-to-talk is worth more than a caps-lock swap.

Window tracking is verified only against the message format the KWin script
sends. There is no KWin session in the sandbox this was written in, so the D-Bus
script load, the `run()` call that KWin 5 needs and KWin 6 does not have, and
the settling under a real alt-tab are all untested against a compositor. The
parsing, the choice of profile and the daemon wiring are tested with a fake bus.
On any other compositor the watcher does not start and the first enabled profile
stays in force.

Stereo to mono is listed in the original notes as a fix for phasing on a mono
jack. It is implemented now, decided on the basis of a measurement rather than
a guess. The reason it is not a filter-chain chain was misdiagnosed once
already: `pactl load-module module-filter-chain` fails with "no such object",
but that does not mean the module is missing. `libpipewire-module-filter-chain.so` is
present in the ostree image, and `pactl list short modules` only ever lists
loaded modules, so its absence from that list proved nothing. The load fails from a real shell too, with the short module name and a graph
copied from the manual, so filter-chain is not reachable through
`pactl load-module` on this machine and the cause is still unknown. Two
independent attempts, both with the same error, so treat that as settled.
filter-chain is also where a compressor would have to live, which is why
compression is delegated to EasyEffects.

It is now established and measured that the two channels of this machine's
microphone really differ, so the original question is settled and the fix is
not a no-op. A control recording of the speakers' monitor comes out bit-identical
left and right, which rules out the measurement chain: whatever differs
afterwards is real. The microphone itself shows a static ~1-2 dB left-over-right
imbalance, phase that drifts towards 180° above 3.5 kHz, and coherence with the
left channel that falls from ~0.9 below 1.5 kHz to ~0.55 by 3.5-8 kHz. Averaging
the two channels into mono is measurably worse than either channel alone:
noise-subtracted, the average is 0.9 dB quieter than the left channel at
50-300 Hz and 3.4 to 6.4 dB quieter in the 1.5-8 kHz band, so an application's
downmix really does cancel a large part of the band. Keeping one channel (the
left, the louder one) keeps the whole band.

So the chain is `module-echo-cancel` - whose output is always stereo, however
its input is wired - feeding a `module-remap-source` that declares
`master_channel_map=front-left channels=1 channel_map=mono`. The virtual
microphone becomes one mono channel, and no application can reintroduce the
cancellation. PipeWire mixes the single channel down by 1/sqrt(2), a flat
-3.0 dB relative to the channel, which `audio.target_volume` above 1.0
compensates for if wanted; `processing.stereo_to_mono: false` restores the
unmodified stereo source.

One finding along the way is worth keeping: `pactl load-module
module-echo-cancel source=...` accepts but ignores the `source` argument in
this PipeWire build (`source_master=...` errors out with "no such object"), and
`module-echo-cancel` cannot point its capture at anything but the default
source. The mono stage does not depend on that: it remaps the echo-cancel
output instead of the physical capture.

## License

MIT.

### The window

`periferia-gui` opens a real window instead of the terminal interface. It shows
what the microphone is doing, straight from the same state file
`periferia status` reads, so there is nothing to keep in step and no second
source of truth.

It edits one thing so far: the keyboard profile. Saving rewrites only the
`profiles` section and keeps the comments, indentation and any section the
window has no opinion about, because a settings window that reflows the rest of
your config on every save is worse than no window at all.

```bash
uv pip install --python .venv/bin/python -e ".[gui]"
.venv/bin/periferia-gui
```

The window has no say over the audio path or the daemon. It is a view of what
they already do, which is why it can be replaced without changing anything
else.
