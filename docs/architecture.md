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
  core/
    config.py            dataclasses, yaml loading, validation
    pipewire.py          every pactl / wp-cli call in the project
    logging_setup.py
    envcheck.py          "can this machine run it"
    daemon.py            wiring and lifecycle
  modules/
    audio.py             virtual mic, volume ramp
    hotkey.py            evdev capture, key name resolution
    processing.py        module-echo-cancel properties
  gui/                   not built yet
```

Two rules the code follows:

1. Nothing outside `core/pipewire.py` runs a subprocess. That keeps the
   sound server interaction in one place and makes it easy to fake in tests.
2. `modules/` has no knowledge of each other. `daemon.py` connects them.

## The gate

PTT is a volume ramp on a virtual source, nothing more.

```
key press   -> ramp up over attack_ms
key release -> wait hold_ms -> ramp down over release_ms
panic       -> volume 0 immediately, hold_ms skipped
```

The ramp is stepped every 5 ms along a curve, because a step change in volume
clicks. `exp` reaches useful loudness fastest in perceived terms, which is why
it is the default even though `linear` is easier to reason about.

If the device is grabbed, or the sound server restarts, the ramp thread
catches the error and logs it rather than dying.

## Why a virtual source

If PTT muted the physical device, then anything else that wanted the microphone
at the same time would be muted too, and there would be no way to ramp
smoothly, because a mute is instant. A `module-loopback` source is created from
the physical one, applications are pointed at the virtual one, and only the
virtual volume moves.

## Where the delay comes from

Periferia adds almost none. The budget is dominated by the PipeWire quantum:
1024 frames at 48 kHz is about 21 ms. Lowering it to 256 in `pipewire.conf`
gives about 5 ms, which is a much bigger win than anything in this codebase.

This is why the audio side of the project is thin on purpose. The work worth
doing here is correctness, not speed.

## What is not in here

- DSP. Noise suppression, echo cancellation and VAD are PipeWire module
  properties. Compressor and de-esser may eventually need LADSPAF.
- Window tracking. On Wayland, compositors do not expose the active window
  freely. This is the main risk for the input-profiles module and should be
  investigated before that work starts.
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
