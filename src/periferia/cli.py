"""Command line entry point."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import select
import shutil
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .core import activewindow, envcheck, pipewire, validate
from .core import config as config_mod
from .core import macro as macro_mod
from .core import state as state_mod
from .gui import model
from .modules import audio as audio_mod
from .modules import hotkey, macrodevice
from .modules.remap import DISABLED_TARGETS

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"


def _supports_color() -> bool:
    return sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if _supports_color() else text


def cmd_check(args: argparse.Namespace) -> int:
    return envcheck.main()


def cmd_devices(args: argparse.Namespace) -> int:
    devices = hotkey.list_input_devices()
    if not devices:
        print("no keyboard devices found in /dev/input/by-id")
        return 1
    for path, name in devices:
        marker = "  " if _readable(path) else "! "
        print(f"{marker}{name}\n     {path}")
    return 0


def _readable(path: Path) -> bool:
    import os

    return os.access(str(path), os.R_OK | os.W_OK)


def cmd_pick_key(args: argparse.Namespace) -> int:
    """Listen for one keypress and print the config value for it."""
    if hotkey.ecodes is None:
        print("evdev is not installed: pip install evdev", file=sys.stderr)
        return 1

    devices = hotkey.find_keyboards("auto", include_pointers=not args.only_keyboards)
    if not devices:
        print("no keyboard device found", file=sys.stderr)
        print("run 'periferia check' to see what is visible and what is blocked", file=sys.stderr)
        return 1

    if len(devices) == 1:
        print(f"listening on {devices[0]}")
    else:
        print(f"listening on {len(devices)} keyboards: {', '.join(str(d) for d in devices)}")
    print("press the key you want for PTT, Esc cancels")
    print("the devices are not grabbed, other programs still see the keypress\n")

    opened: dict[int, Any] = {}
    try:
        for path in devices:
            try:
                dev = hotkey.open_device(path)
            except OSError as exc:
                print(f"cannot open {path}: {exc}", file=sys.stderr)
                continue
            opened[dev.fd] = dev
    except Exception:
        pass
    if not opened:
        print("none of the keyboards could be opened", file=sys.stderr)
        print("this usually means missing permissions, see docs/udev.md", file=sys.stderr)
        return 1

    deadline = time.monotonic() + args.timeout
    try:
        while time.monotonic() < deadline:
            # The same select() plus read() the daemon uses. evdev 2.0 yields
            # single InputEvent objects from read(), while the 1.x API this
            # used to call returned (device, event) pairs, and unpacking one
            # of those objects failed before any key could be picked.
            try:
                readable, _, _ = select.select(list(opened), [], [], 0.5)
            except (OSError, ValueError):
                readable = []
            for fd in readable:
                dev = opened[fd]
                for event in dev.read():
                    if event.type != hotkey.ecodes.EV_KEY or event.value != hotkey.PRESS:
                        continue
                    name = hotkey.code_name(event.code)
                    if name == "KEY_ESC":
                        print("cancelled")
                        return 130
                    label = hotkey.key_label(event.code)
                    print(f"\n  key   {name}")
                    print(f"  label {label}")
                    print(f"\nyaml:   ptt_key: \"{name}\"")
                    return 0
        print("\nnothing pressed, giving up", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ncancelled")
        return 130
    finally:
        for dev in opened.values():
            with contextlib.suppress(OSError):
                dev.close()
    return 1


def _open_keyboards(what: str) -> dict[int, Any]:
    """Open every readable keyboard, or exit with a reason why not."""
    if hotkey.ecodes is None:
        print("evdev is not installed: pip install evdev", file=sys.stderr)
        raise SystemExit(1)
    devices = hotkey.find_keyboards("auto", include_pointers=False)
    if not devices:
        print("no keyboard device found", file=sys.stderr)
        print("run 'periferia check' to see what is visible and what is blocked", file=sys.stderr)
        raise SystemExit(1)
    opened: dict[int, Any] = {}
    for path in devices:
        try:
            dev = hotkey.open_device(path)
        except OSError as exc:
            print(f"cannot open {path}: {exc}", file=sys.stderr)
            continue
        opened[dev.fd] = dev
    if not opened:
        print(f"none of the keyboards could be opened, so {what} is not possible", file=sys.stderr)
        print("this usually means missing permissions, see docs/udev.md", file=sys.stderr)
        raise SystemExit(1)
    return opened


def _macro_rows(
    cfg: config_mod.Config, names: Sequence[str] = ()
) -> tuple[list[Any], list[tuple[str, str]]]:
    """The macros that can be built, plus the ones that cannot and why.

    A macro with an unresolvable key name must not take the others down with it.
    Listing is how someone finds out what is wrong, so raising here would replace
    the list with a stack trace and hide the twelve macros that are fine. The
    failure is returned instead, and 'macro check' is what reports it properly.
    """
    broken: list[tuple[str, str]] = []
    out: list[Any] = []
    entries = list(cfg.macros)
    for profile in cfg.profiles:
        if profile.enabled:
            entries.extend(profile.macros)
            break
    for entry in entries:
        name = getattr(entry, "name", "") or "?"
        try:
            out.append(macro_mod.from_config(entry, _resolve_macro_key))
        except macro_mod.MacroError as exc:
            broken.append((name, str(exc)))
    if names:
        wanted = set(names)
        out = [m for m in out if m.name in wanted]
    return out, broken


def _resolve_macro_key(name: str) -> int | None:
    return hotkey.resolve_key(str(name).strip().upper())


def _key_name(code: int) -> str:
    return hotkey.code_name(code)


def cmd_macro(args: argparse.Namespace) -> int:
    conf = config_mod.load(args.config)
    reserved = [conf.ptt.ptt_key, conf.ptt.panic_key]
    action = args.macro_command

    if action in ("list", "check"):
        report = validate.check_macros(conf.macros, conf.profiles, reserved=reserved)
        if action == "check":
            return _print_report(report)

        macros, broken = _macro_rows(conf)
        print(f"{BOLD}macros{RESET}")
        if not macros and not broken:
            print(f"  {DIM}none configured{RESET}")
            print(f"  {DIM}record one with: periferia macro record NAME{RESET}")
        for m in macros:
            bind = m.bind or _c("not on a key", YELLOW)
            summary = macro_mod.describe(m, _key_name)
            print(f"  {m.name:16} {_c(bind, CYAN)}  {_c(summary, DIM)}")
        for name, why in broken:
            print(f"  {_c('could not read', RED)} {name}  {_c(why, DIM)}")
        problems = [p for p in report.problems if p.level == validate.ERROR]
        if problems:
            print(f"\n  {_c(f'{len(problems)} that will not play', RED)}")
            print(f"  {DIM}run: periferia macro check{RESET}")
        return 0

    if action == "play":
        macros, broken = _macro_rows(conf)
        for name, why in broken:
            if name == args.name:
                print(f"macro {name!r} cannot be read: {why}", file=sys.stderr)
                return 1
        wanted = [m for m in macros if m.name == args.name]
        if not wanted:
            print(f"no macro named {args.name!r}", file=sys.stderr)
            known = ", ".join(m.name for m in macros) or "none"
            print(f"known: {known}", file=sys.stderr)
            return 1
        macro = wanted[0]
        try:
            steps = macro_mod.plan(
                macro,
                _resolve_macro_key(macro.bind) if macro.bind else None,
                {c for c in (_resolve_macro_key(r) for r in reserved) if c is not None},
            )
        except macro_mod.MacroError as exc:
            print(f"cannot play: {exc}", file=sys.stderr)
            return 1
        player = macrodevice.MacroPlayer()
        if not player.play(steps):
            print("nothing played", file=sys.stderr)
            return 1
        while player.playing:
            time.sleep(0.02)
        player.close()
        return 0

    if action == "record":
        return _macro_record(args, conf)

    if action == "delete":
        return _macro_delete(args, conf)

    print(f"unknown macro command {action!r}", file=sys.stderr)
    return 2


def _macro_record(args: argparse.Namespace, conf: config_mod.Config) -> int:
    stop_code = hotkey.resolve_key(args.stop_key.upper())
    if stop_code is None:
        print(f"unknown stop key {args.stop_key!r}", file=sys.stderr)
        return 1
    recorder = macro_mod.Recorder(ignore=frozenset({stop_code}))
    opened = _open_keyboards("recording")
    print(f"recording into {BOLD}{args.name}{RESET}")
    print(f"press the keys you want, then {BOLD}{args.stop_key}{RESET} to stop, Esc cancels")
    print("the keyboards are not grabbed, other programs still see every key\n")
    deadline = time.monotonic() + args.timeout
    try:
        while time.monotonic() < deadline:
            try:
                readable, _, _ = select.select(list(opened), [], [], 0.5)
            except (OSError, ValueError):
                readable = []
            for fd in readable:
                dev = opened[fd]
                for event in dev.read():
                    if event.type != hotkey.ecodes.EV_KEY:
                        continue
                    now = time.monotonic()
                    if event.code == stop_code and event.value == hotkey.RELEASE:
                        return _macro_finish(args, conf, recorder, opened, cancelled=False)
                    if event.code == hotkey.ecodes.KEY_ESC and event.value == hotkey.PRESS:
                        return _macro_finish(args, conf, recorder, opened, cancelled=True)
                    if event.value in (hotkey.PRESS, hotkey.RELEASE):
                        recorder.feed(now, event.code, event.value)
    except KeyboardInterrupt:
        print("\ncancelled")
        return 130
    finally:
        for dev in opened.values():
            with contextlib.suppress(OSError):
                dev.close()
    print("\nnothing recorded before the timeout", file=sys.stderr)
    return 1


def _macro_finish(
    args: argparse.Namespace,
    conf: config_mod.Config,
    recorder: macro_mod.Recorder,
    opened: dict[int, Any],
    cancelled: bool,
) -> int:
    for dev in opened.values():
        with contextlib.suppress(OSError):
            dev.close()
    steps = recorder.finish()
    if cancelled:
        print("\ncancelled")
        return 130
    if not steps:
        print("\nnothing was recorded, the config was not changed", file=sys.stderr)
        return 1

    print(f"\n  {_c(f'{len(steps)} keypresses', GREEN)}")
    shown = " ".join(hotkey.code_name(s.code) for s in steps[:12])
    if len(steps) > 12:
        shown += f" +{len(steps) - 12} more"
    print(f"  {DIM}{shown}{RESET}")

    bind = args.bind
    if bind is None:
        # Only ask when there is someone there to answer. Piped into a script,
        # the prompt would read the next line of the script as a key name.
        bind = (
            input("  key to play it on (blank for none): ").strip().upper()
            if sys.stdin.isatty()
            else ""
        )
    if bind and _resolve_macro_key(bind) is None:
        print(f"  {bind!r} is not a key this project knows, saving it unbound", file=sys.stderr)
        bind = ""

    path = conf.path or config_mod.default_config_path()
    try:
        model.save_macro(
            path,
            args.name,
            [(hotkey.code_name(s.code), s.at_ms, s.hold_ms) for s in steps],
            bind=bind,
            profiles=list(args.in_profile or ()),
        )
    except OSError as exc:
        print(f"cannot write {path}: {exc}", file=sys.stderr)
        return 1
    print(f"  saved to {path}")
    print(f"  {_c('check it with: periferia macro check', DIM)}")
    return 0


def _macro_delete(args: argparse.Namespace, conf: config_mod.Config) -> int:
    path = conf.path or config_mod.default_config_path()
    data = model.load_raw(path)
    found = False
    container = data if isinstance(data, dict) else {}
    for profile in container.get("profiles") or []:
        if not isinstance(profile, dict):
            continue
        kept = [m for m in (profile.get("macros") or []) if m.get("name") != args.name]
        if len(kept) != len(profile.get("macros") or []):
            found = True
            profile["macros"] = kept
    kept = [m for m in (container.get("macros") or []) if m.get("name") != args.name]
    if len(kept) != len(container.get("macros") or []):
        found = True
        container["macros"] = kept
    if not found:
        print(f"no macro named {args.name!r} in {path}", file=sys.stderr)
        return 1
    import yaml as pyyaml

    path.write_text(pyyaml.safe_dump(container, sort_keys=False), encoding="utf-8")
    print(f"deleted {args.name!r} from {path}")
    return 0


def _print_report(report: validate.Report) -> int:
    """Print a report the same way for profiles and for macros.

    Two copies of this drifted once already: the macro one grew a wording
    difference that made the same severity read differently depending on which
    command found it.
    """
    if not report.problems:
        print(f"\n  {_c('nothing to report', GREEN)}")
        return 0
    print()
    for problem in report.problems:
        colour = RED if problem.level == validate.ERROR else YELLOW
        print(f"  {_c(problem.level, colour)}  {problem.where}: {problem.message}")
    print()
    if not report.ok:
        print(f"  {_c(f'{len(report.errors)} that will stop it working', RED)}")
    if report.warnings:
        print(f"  {_c(f'{len(report.warnings)} worth knowing', YELLOW)}")
    return 1


def cmd_profiles(args: argparse.Namespace) -> int:
    conf = config_mod.load(args.config)
    report = validate.check_profiles(
        conf.profiles,
        reserved=[conf.ptt.ptt_key, conf.ptt.panic_key],
    )
    print(f"{BOLD}profiles{RESET}")
    if not conf.profiles:
        print(f"  {DIM}none configured{RESET}")
    for profile in conf.profiles:
        match = ", ".join(f"{k}={v}" for k, v in (profile.match or {}).items()) or "fallback"
        off = "" if profile.enabled else f" {DIM}(disabled){RESET}"
        keys = len(profile.remap)
        # A key turned off is a hole in the keyboard, so it is named here rather
        # than only counted with the remaps.
        killed = [str(k) for k, v in (profile.remap or {}).items() if _is_off(v)]
        suffix = f" {DIM}{keys} keys" + (f", {len(killed)} off{RESET}" if killed else "")
        names = f" {DIM}({' '.join(killed)}){RESET}" if killed else ""
        speed = getattr(getattr(profile, "pointer", None), "speed", None)
        if speed is not None:
            suffix += f", pointer {speed:g}x"
        print(f"  {profile.name:16} {match}{off} {suffix}{names}")
    return _print_report(report)


def _is_off(target: object) -> bool:
    """Whether a remap target means "this key does not exist"."""
    return str(target).strip().lower() in DISABLED_TARGETS


def cmd_probe_window(args: argparse.Namespace) -> int:
    bus = activewindow.inspect_bus()
    print(f"{BOLD}session bus{RESET}")
    if bus.is_flatpak_proxy:
        print(f"  {_c('wrong bus', BOLD + YELLOW)}: {bus.detail}")
    else:
        print(f"  {bus.detail}")
    if bus.has_real_socket:
        print(f"  a real socket exists at {os.environ.get('XDG_RUNTIME_DIR')}/bus")

    print(f"\n{BOLD}active window{RESET}")
    def show(later: activewindow.WindowReport) -> None:
        print(f"  {DIM}->{RESET}")
        for name, value in later.interesting():
            print(f"  {name:16} {value}")
        if not later.interesting():
            print(f"  {DIM}(nothing usable){RESET}")

    print(
        f"  {DIM}asking KWin, up to {args.timeout:.0f}s...{RESET}",
        flush=True,
    )
    report = activewindow.probe(
        timeout=args.timeout, on_report=show if args.watch else None
    )
    if report.status == envcheck.OK:
        print(f"  {_c('found', BOLD + GREEN)}: {report.detail}")
    elif report.status == envcheck.WARN:
        print(f"  {_c('partial', BOLD + YELLOW)}: {report.detail}")
    else:
        print(f"  {_c('no', BOLD + YELLOW)}: {report.detail}")
    if report.hint:
        for line in report.hint.splitlines():
            print(f"  {DIM}{line}{RESET}")

    if report.interesting():
        print(f"\n{BOLD}what the compositor sent{RESET}")
        for name, value in report.interesting():
            print(f"  {name:16} {value}")
    if report.fields:
        silent = report.silent()
        if silent:
            print(f"\n  {DIM}nothing for: {', '.join(silent)}{RESET}")
    matchable = report.matchable()
    if matchable:
        print(f"\n  a profile could match on: {_c(', '.join(matchable), GREEN)}")
    elif report.fields:
        print(f"\n  {YELLOW}no field a profile could match on{RESET}")
    if not args.watch:
        print(
            f"\n  {DIM}this is one instant, and running a command from a terminal"
            f" means the\n  terminal is what was active. To see any other window,"
            f" use --watch\n  and switch while it runs.{RESET}"
        )
    if args.watch and not report.interesting():
        print(
            f"\n  {YELLOW}nothing came back, so there is nothing to watch.{RESET}"
            f"\n  {DIM}Fix the failure above first; watching an absent window only"
            f" waits forever.{RESET}"
        )
        return 1
    if args.watch:
        print(f"\n  {DIM}watching, switch windows, Ctrl-C to stop{RESET}", flush=True)
        with contextlib.suppress(KeyboardInterrupt):
            waited = 0
            while True:
                time.sleep(1)
                waited += 1
                if waited % 15 == 0:
                    print(
                        f"  {DIM}still alive after {waited}s, nothing has changed"
                        f" focus{RESET}",
                        flush=True,
                    )
    return 0 if report.status == envcheck.OK else 1


def cmd_list_sources(args: argparse.Namespace) -> int:
    try:
        items = pipewire.sources()
    except pipewire.PipeWireError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not items:
        print("no sources")
        return 1

    for item in items:
        name = item.get("name") or "?"
        tag = "virtual" if pipewire.is_virtual(name) else "physical"
        muted = "muted" if item.get("mute") else ""
        print(f"[{tag:7}] {name}  {muted}")

    default = pipewire.default_source()
    print(f"\ndefault source: {default or 'unknown'}")
    print(f"physical mic  : {audio_mod.pick_physical_source('auto') or 'none found'}")
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    """Show the graph as pw-dump reports it, the way cleanup reads it."""
    if not pipewire.have_graph():
        print("pw-dump not found", file=sys.stderr)
        return 1
    try:
        graph = pipewire.dump()
    except pipewire.PipeWireError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"{BOLD}nodes{DIM}")
    for node in graph.nodes:
        media = node.media_class or "?"
        module = str(node.pulse_module_id) if node.pulse_module_id is not None else "-"
        print(f"  {node.id:<6} {media:<24} {node.name or '?'}  [{module}]")
        if node.description:
            print(f"  {'':<6} {'':<24} {DIM}{node.description}{DIM}")

    duplicates = graph.duplicates()
    if duplicates:
        print(f"\n{_c('duplicate names', YELLOW)}")
        for name, ids in duplicates.items():
            print(f"  {name}: {', '.join(str(i) for i in ids)}")

    print(f"\n{BOLD}links{DIM}")
    for link in graph.links:
        print(
            f"  {link.id:<6} {link.output_node}:{link.output_port}"
            f" -> {link.input_node}:{link.input_port}  {link.state or '?'}"
        )
    if not graph.links:
        print(f"  {DIM}none{DIM}")
    return 0


def cmd_set_default(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    name = args.name
    if not name:
        # The virtual source is created with our description but gets a
        # machine-generated name, so resolve it through that description.
        name = pipewire.find_source(cfg.audio.virtual_name) or ""
        if not name:
            print(
                f"no source described as {cfg.audio.virtual_name!r}, "
                "is the daemon running?",
                file=sys.stderr,
            )
            print("start it with 'periferia-daemon', or pass a name explicitly", file=sys.stderr)
            return 1
    if not pipewire.source_exists(name):
        print(f"{name} does not exist. Run 'periferia sources'", file=sys.stderr)
        return 1
    try:
        pipewire.set_default_source(name)
    except pipewire.PipeWireError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"default source set to {name}")
    return 0


def cmd_gate_check(args: argparse.Namespace) -> int:
    """Watch the gate move, without needing a microphone to work.

    Everything up to the last step can be proved without sound: if the virtual
    source is there and its volume follows the key, then the daemon heard the
    key, the ramp ran, and pactl accepted the write. Whether any application
    opens that source is a separate question that only a recording answers.

    The point is to stop "PTT did nothing" from having three possible causes.
    """
    note = state_mod.read()
    name = getattr(args, "source", None) or (note or {}).get("source") or ""
    if not name:
        print(
            "no source to watch. Start the daemon, or pass one: "
            "periferia gate-check NAME",
            file=sys.stderr,
        )
        return 1

    before = pipewire.get_volume(name)
    if before is None:
        print(f"{name} does not exist right now. Is the daemon running?", file=sys.stderr)
        print("start it, or run 'periferia teardown' and restart it", file=sys.stderr)
        return 1

    running = state_mod.is_running(note)
    if not running:
        print("warning: no daemon is running, so nothing is going to move\n", file=sys.stderr)

    print(f"watching {name}, currently at {before:.0%}")
    print("press and hold the PTT key. Ctrl+C to give up.\n")

    peak = before
    limit = getattr(args, "timeout", 30.0)
    deadline = time.monotonic() + limit
    try:
        while time.monotonic() < deadline:
            level = pipewire.get_volume(name)
            if level is not None and level > peak:
                peak = level
                print(f"  moved to {level:.0%}")
            if peak > 0.5:
                break
            time.sleep(0.05)
    except KeyboardInterrupt:
        print()
        print("gave up before the gate moved")
        return 1

    if peak <= 0.5:
        print(f"still at {peak:.0%} after {limit:.0f}s: the gate did not move")
        print("check the daemon's log for 'volume update failed'", file=sys.stderr)
        return 1

    print(f"\nthe gate moved: {before:.0%} -> {peak:.0%}")
    print("the key, the daemon and pactl are all working.")
    print("what is left is whether an application opens this source.")
    return 0


def cmd_teardown(args: argparse.Namespace) -> int:
    """Unload echo-cancel modules left behind by a crash."""
    try:
        items = pipewire.sources()
    except pipewire.PipeWireError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    seen: dict[int, str] = {}
    for item in items:
        module_id = item.get("owner_module")
        if module_id and module_id != pipewire.NO_MODULE:
            seen[module_id] = item.get("name") or "?"

    count = 0
    for module_id, name in sorted(seen.items()):
        if pipewire.unload_module(module_id):
            count += 1
            print(f"unloaded module {module_id} ({name})")
    print(f"{count} modules unloaded")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Say whether the microphone is live, without drawing a tray icon.

    The roadmap asks for a tray that shows an open microphone. A Qt tray is
    about 80 MB of dependencies for one dot, so the daemon leaves a note
    instead and this prints it. Anything that wants the same information can
    read the same file.
    """
    note = state_mod.read()
    if note is None:
        print(f"{DIM}periferia is not running{DIM}")
        print(f"{DIM}no state at {state_mod.state_path()}{DIM}")
        return 1

    if not state_mod.is_running(note):
        # The note survives its author, so report the corpse rather than the
        # last thing it managed to say.
        pid = note.get("pid", "?")
        when = state_mod.age(note)
        ago = f" {when:.0f}s ago" if when is not None else ""
        print(f"{YELLOW}periferia is not running{DIM}")
        was = note.get("state")
        print(f"{DIM}it stopped while the microphone was '{was}'{ago} (pid {pid}){DIM}")
        print(f"{DIM}stale note at {state_mod.state_path()}{DIM}")
        return 1

    now = note.get("state")
    since = note.get("changed_at")
    ago = ""
    if isinstance(since, (int, float)):
        ago = f", {max(0.0, time.time() - since):.1f}s ago"

    if now == state_mod.OPEN:
        colour, word = GREEN, "OPEN"
    elif now == state_mod.LATCHED:
        # Latched is the one state nobody can hear, so it has to be readable.
        colour, word = CYAN, "LATCHED, press ptt to close"
    elif now == state_mod.CLOSING:
        colour, word = YELLOW, "closing"
    elif now == state_mod.PANIC:
        colour, word = YELLOW, "panic"
    else:
        colour, word = DIM, "closed"
    print(f"microphone {_c(word, colour)}{ago}")
    print(f"source        {note.get('source') or 'unknown'}")
    print(f"daemon pid    {note.get('pid', '?')}")
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    from .tui import run

    return run(config_mod.load(args.config))


def cmd_ramp(args: argparse.Namespace) -> int:
    """Manual volume ramp, to check for clicks and latency before PTT exists."""
    import time

    cfg = config_mod.load(args.config)
    line = args.name or audio_mod.pick_physical_source(cfg.audio.physical_source) or ""
    if not pipewire.source_exists(line):
        print(f"no usable source (got {line!r})", file=sys.stderr)
        return 1

    mic = audio_mod.VirtualMic(cfg.audio)
    mic._source = line  # manual test path, bypasses setup

    target = float(args.volume)
    duration = int(args.ms)
    print(f"ramping {line} -> {target} over {duration} ms")
    try:
        mic.ramp(target, duration)
        deadline = time.monotonic() + duration / 1000.0 + 1.0
        while time.monotonic() < deadline:
            time.sleep(0.02)
            thread = mic._ramp_thread
            if thread is None or not thread.is_alive():
                break
        print(f"done, volume is {mic.volume:.2f}")
    finally:
        with contextlib.suppress(pipewire.PipeWireError):
            pipewire.set_volume(line, 0.0)
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(config_mod.load(args.config).to_dict(), indent=2, ensure_ascii=False))
        return 0
    path = config_mod.find_config(args.config)
    print(f"config: {path or 'defaults (no file found)'}")
    for section, values in config_mod.load(args.config).to_dict().items():
        print(f"\n[{section}]")
        for key, value in values.items():
            print(f"  {key} = {value!r}")
    return 0

def cmd_init(args: argparse.Namespace) -> int:
    target = config_mod.default_config_path()
    if target.exists() and not args.force:
        print(f"{target} already exists, use --force to overwrite")
        return 1
    template = Path(__file__).resolve().parent.parent.parent / "config.example.yaml"
    if template.is_file():
        target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"wrote {target}")
    print("next: periferia check")
    return 0


def _daemon_script() -> Path | None:
    """Locate the installed console script.

    The unit shipped in the repo cannot hardcode a path: a venv puts it next to
    the interpreter, a user install puts it in ~/.local/bin, and a distro
    package puts it in /usr/bin.
    """
    candidates = [Path(sys.executable).parent / "periferia-daemon"]
    on_path = shutil.which("periferia-daemon")
    if on_path:
        candidates.append(Path(on_path))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def cmd_install_service(args: argparse.Namespace) -> int:
    unit = Path(__file__).resolve().parent.parent.parent / "systemd" / "periferia.service"
    if not unit.is_file():
        print(f"{unit} not found", file=sys.stderr)
        return 1
    daemon = _daemon_script()
    if daemon is None:
        print(
            "could not find the installed 'periferia-daemon' script; "
            "install the package or add its bin directory to PATH",
            file=sys.stderr,
        )
        return 1
    dest_dir = Path.home() / ".config" / "systemd" / "user"
    if not shutil.which("systemctl"):
        print("systemctl not found, copy the unit manually", file=sys.stderr)
        return 1
    dest_dir.mkdir(parents=True, exist_ok=True)

    # The unit makes this writable for the daemon, and systemd refuses to build
    # the mount namespace for a ReadWritePaths entry that does not exist yet.
    (Path.home() / ".config" / "periferia").mkdir(parents=True, exist_ok=True)

    dest = dest_dir / unit.name
    text = re.sub(
        r"^ExecStart=.*$",
        f"ExecStart={daemon}",
        unit.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    dest.write_text(text, encoding="utf-8")
    print(f"wrote {dest}")
    print("run: systemctl --user daemon-reload && systemctl --user enable --now periferia")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="periferia",
        description="Global push-to-talk and microphone control for Linux",
    )
    parser.add_argument("-c", "--config", help="path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="verify this machine can run periferia").set_defaults(
        func=cmd_check
    )
    sub.add_parser("devices", help="list input devices that look like keyboards").set_defaults(
        func=cmd_devices
    )
    pick = sub.add_parser("pick-key", help="press a key, get the config value for it")
    pick.add_argument("--timeout", type=float, default=30.0, help="give up after this many seconds")
    pick.add_argument(
        "--only-keyboards",
        action="store_true",
        help="ignore mouse buttons, only listen to typing keys",
    )
    pick.set_defaults(func=cmd_pick_key)
    sub.add_parser("sources", help="list PipeWire sources").set_defaults(func=cmd_list_sources)
    sub.add_parser(
        "graph",
        help="show the PipeWire graph the way cleanup reads it",
    ).set_defaults(func=cmd_graph)
    gate = sub.add_parser(
        "gate-check",
        help="watch the gate move on the PTT key, needs no microphone",
    )
    gate.add_argument("source", nargs="?", help="source to watch, defaults to the daemon's")
    gate.add_argument("--timeout", type=float, default=30.0, help="seconds to wait, default 30")
    gate.set_defaults(func=cmd_gate_check)
    sub.add_parser(
        "profiles",
        help="show the configured profiles and what is wrong with them",
    ).set_defaults(func=cmd_profiles)

    macro_cmd = sub.add_parser("macro", help="record, list and check macros")
    macro_sub = macro_cmd.add_subparsers(dest="macro_command")

    macro_sub.add_parser("list", help="show the macros and what they do").set_defaults(
        func=cmd_macro
    )
    macro_sub.add_parser("check", help="say what is wrong with the macros").set_defaults(
        func=cmd_macro
    )

    play = macro_sub.add_parser("play", help="play one macro now")
    play.add_argument("name", help="the macro to play")
    play.set_defaults(func=cmd_macro)

    record = macro_sub.add_parser("record", help="record a macro from the keyboard")
    record.add_argument("name", help="what to call it")
    record.add_argument(
        "--bind",
        default=None,
        help="the key that will play it, e.g. KEY_F5. Asked for if not given",
    )
    record.add_argument(
        "--stop-key",
        default="KEY_F12",
        help="the key that ends the recording (default: KEY_F12)",
    )
    record.add_argument(
        "--in-profile",
        action="append",
        metavar="NAME",
        help="also put a copy in this profile, may be given more than once",
    )
    record.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="give up after this many seconds (default: 60)",
    )
    record.set_defaults(func=cmd_macro)

    delete = macro_sub.add_parser("delete", help="remove a macro")
    delete.add_argument("name", help="the macro to remove")
    delete.set_defaults(func=cmd_macro)

    probe = sub.add_parser(
        "probe-window",
        help="find out whether the compositor can name the focused window",
    )
    probe.add_argument(
        "--timeout",
        type=float,
        default=12.0,
        help="how long to wait for the compositor to answer, default 12",
    )
    probe.add_argument(
        "--watch",
        action="store_true",
        help="keep listening and print every window as it takes focus",
    )
    probe.set_defaults(func=cmd_probe_window)
    sub.add_parser("teardown", help="unload leftover echo-cancel modules").set_defaults(
        func=cmd_teardown
    )
    sub.add_parser("status", help="show whether the microphone is live").set_defaults(
        func=cmd_status
    )
    sub.add_parser("init-config", help="write a starter config").set_defaults(func=cmd_init)

    p = sub.add_parser("set-default", help="point the default source at a device")
    p.add_argument("name", nargs="?", help="source name, default is the physical mic")
    p.set_defaults(func=cmd_set_default)

    p = sub.add_parser("ramp", help="manual volume ramp, to test for clicks")
    p.add_argument("name", nargs="?", help="source name")
    p.add_argument("-v", "--volume", default="0.5", help="target volume 0..1")
    p.add_argument("-m", "--ms", default="200", help="duration in ms")
    p.set_defaults(func=cmd_ramp)

    p = sub.add_parser("config", help="show the effective config")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("install-service", help="install the systemd user unit")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_install_service)

    p = sub.add_parser("tui", help="interactive self check in the terminal")
    p.set_defaults(func=cmd_tui)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        if args.command not in ("check", "pick-key"):
            print(f"{_c('error', BOLD + YELLOW)}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
