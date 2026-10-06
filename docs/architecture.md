# Architecture

## The core idea

Periferia does not control Discord. It controls PipeWire. The microphone is
closed unless a key is held, and that is true for every application at once,
because the decision happens in the sound server rather than inside any one
program.

That is also why the Flatpak version of Discord works without special handling.
Discord is sandboxed and cannot read `/dev/input`, but it does not need to.
It just sees a system microphone that is sometimes quiet.

## Layout

```
src/periferia/
  cli.py                 command line
  tui.py                 terminal window
  core/
    config.py            dataclasses, yaml loading, validation
    pipewire.py          every pactl / pw-dump call in the project
    graph.py             parses pw-dump: nodes, ports, links, module ids
    logging_setup.py
    envcheck.py          "can this machine run it"
    state.py             the state file `periferia status` reads
    macro.py             macro model, recording, playback timeline
    keyboards.py         which keyboards, remembered across runs
    watcher.py           notices that the config file changed
    windowprofile.py     picks a profile for the focused window
    activewindow.py      the compositor, where it can be asked
    windowbus.py         the bus name a compositor script reports to
    windowwatch.py       the loaded KWin script, polled and settled
    kwinconfig.py        the pointer speed, written where KWin reads it
    validate.py          what will not work, and why
    daemon.py            wiring and lifecycle
  modules/
    audio.py             virtual mic, volume ramp
    hotkey.py            evdev capture, key name resolution
    processing.py        module-echo-cancel properties
    macrodevice.py       replays macros through uinput
    remap.py             which key becomes which key
    router.py            grabs the keyboard and remaps what it emits
  gui/
    model.py             what the window can change: one profile, as a draft
    window.py            the pages, and the profile editor
    pages.py             read-only views of state, profiles and problems
    tray.py              tray icon and its state file
```

`gui/model.py` is the only place that knows how a profile is written. The
widgets hold a `ProfileDraft` and hand it over; nothing in `window.py` builds a
config. Two reasons: the editor is a table, and the file is a document with
comments in it, so the two are kept apart.

The draft carries only `name`, `match`, `remap` and `pointer`. Everything else
in a profile -- its macros, its `enabled` flag -- is in the file and is left
there by `save_draft`, which rewrites the one profile it was given through
ruamel's round-trip loader. That is why editing a profile from the window cannot
delete a macro it does not show.

Three rules the code follows:

1. Nothing outside `core/pipewire.py` runs a subprocess. That keeps the
   sound server interaction in one place and makes it easy to fake in tests.
2. `modules/` has no knowledge of each other. `daemon.py` connects them.
3. The daemon owns everything that must survive a reload, and reloads as little
   as possible. A config change re-reads macros and rebuilds the keyboard, and
   leaves the microphone, the virtual source and the audio processing alone:
   those own a PipeWire node that applications are pointed at.

## The gate

PTT is a volume ramp on a virtual source, nothing more.

```
key press   -> ramp up over attack_ms
key release -> wait hold_ms -> ramp down over release_ms
panic       -> volume 0 immediately, hold_ms skipped
```

The ramp is stepped along a curve, because a step change in volume clicks.
Progress comes from the clock, not from a step counter: one write against
`pactl` costs a fork and a round trip, well over the 5 ms the ramp would like
to allow, so counting writes stretched a 200 ms attack into most of a second
and the mic was heard opening late. A write that overruns a tick skips it
rather than catching up, and the last value written is the target exactly.
`exp` reaches useful loudness fastest in perceived terms, which is why it is
the default even though `linear` is easier to reason about.

If the device is grabbed, or the sound server restarts, the ramp thread
catches the error and logs it rather than dying.

## Why a virtual source

If PTT muted the physical device, then anything else that wanted the microphone
at the same time would be muted too, and there would be no way to ramp
smoothly, because a mute is instant. `module-echo-cancel` is fed from the
physical device and publishes its own virtual source, renamed through
`source_properties` to the name applications look for. Applications point at
that one and only its volume moves.

## Where the delay comes from

Periferia adds almost none. The budget is dominated by the PipeWire quantum:
1024 frames at 48 kHz is about 21 ms. Lowering it to 256 in `pipewire.conf`
gives about 5 ms, which is a much bigger win than anything in this codebase.

This is why the audio side of the project is thin on purpose. The work worth
doing here is correctness, not speed.

## Reading the graph

A killed run leaves its `module-echo-cancel` loaded, and the next run then has
two nodes answering to one name. `pactl` resolves a name to whichever comes
first, so the gate drives one source while the self check reads the other and
the level sits frozen.

`pw-dump` answers this directly. A node carries the description we asked for
and the `pulse.module.id` it was created by, so cleanup reads the id off the
node instead of matching it back against a module list -- `pactl list short
modules` has no id field at all, which is why this used to find nothing. The
module is still confirmed to be ours through its argument before anything is
unloaded, so a description that happens to match someone else's module cannot
get it torn down.

`pw-dump` occasionally prints an array with no key inside a `params` map,
which is not valid JSON. `core/graph.py` names those arrays and parses the
rest; when the read cannot be salvaged, or `pw-dump` is not installed, the
caller gets `None` and falls back to the pactl path. Nothing outside
`pipewire.py` runs a subprocess, so `graph.py` is a pure parser.

Writes still go through `pactl`. Loading a module is its job, it is the
supported way to do it, and the graph is read-only here.

## What is not in here

- DSP. Noise suppression, echo cancellation and VAD are PipeWire module
  properties. Compressor and de-esser may eventually need LADSPAF.
- Window tracking on anything but KWin. On Wayland, compositors do not expose
  the active window freely, so `windowwatch` loads a KWin script and speaks
  `org.kde.KWin.Scripting` over the session bus. GNOME and X11 are not
  supported: a config with `match` there falls back to the first enabled
  profile and says so in the log.
- RGB. Needs per-vendor HID protocols, many of them undocumented. Likely a
  research task rather than an engineering one.

## Next steps, in order

1. Confirm the virtual mic works and is audible.
2. Confirm a manual ramp causes no clicks and no audible delay.
3. PTT: connect the key listener to the gate. This is the whole product.
4. Noise suppression via module properties.
5. Tray indicator and panic key.
6. Packaging.

Steps 1 and 2 are the ones worth being patient with. Everything after them is
straightforward once the mic is proven to work.
