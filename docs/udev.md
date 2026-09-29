# Input device access

Periferia reads key events from `/dev/input/eventN`. If `periferia check`
already says the devices are readable, stop here, nothing needs doing.

## Check first

```bash
periferia check
periferia devices
ls -l /dev/input/by-id/
getfacl /dev/input/event5
```

On a normal desktop session, logind puts an ACL on every input device belonging
to the active seat, so the logged-in user can usually read and write them
already. That is why a fresh install often just works.

You only need the rules below if the check shows devices as unreadable, or if
`periferia pick-key` fails with a permission error.

## If you do need them

Find the vendor and product ids of your keyboard:

```bash
udevadm info -a -n /dev/input/event5 | grep -E 'id=|PRODUCT='
```

Put those numbers into `udev/10-periferia.rules`, then:

```bash
sudo cp udev/10-periferia.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Log out and back in, then run `periferia check` again.

`TAG+="uaccess"` is the important part. It hands the device to whoever is
logged in on the active seat, which is the same mechanism used for
`/dev/snd`. It is narrower than `MODE="0666"` and does not give every user on
the machine access to your keyboard.

## Why not just run as root

A program that can read every keystroke on the machine, and can create virtual
input devices, has no business running as root. If the daemon is installed as a
systemd user service it runs with your own privileges and the rules above are
enough.

The unit in `systemd/periferia.service` already sets `NoNewPrivileges` and
`ProtectSystem=strict`.

## Debugging reads

```bash
sudo evtest /dev/input/event5          # interactive, prints raw events
sudo fuser -v /dev/input/event5        # who else is grabbing it
cat /proc/bus/input/devices            # all input devices and their handles
```

If `evtest` shows your keypresses but Periferia does not, something else has
the device grabbed, or the device node in the config is stale. Device nodes
change across reboots, which is why the config resolves devices through
`/dev/input/by-id` instead of hardcoding `eventN`.
