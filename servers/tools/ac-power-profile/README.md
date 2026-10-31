# ac-power-profile: full speed on the charger, power-saver on battery

For a Linux laptop you carry (raven). Plugged in, it sets the `performance` power profile; on battery,
`power-saver`. It re-applies on every plug and unplug (a udev rule) and at boot, so you never switch by hand.
While [host-heat](../host-heat) is cooling the machine it stands aside: host-heat owns the profile until it
has cooled down and put the old one back.

Linux only, with power-profiles-daemon (`powerprofilesctl`; most desktops ship it, Omarchy does).

## Install

From the kit's root folder:

```sh
sudo servers/tools/ac-power-profile/install.sh
```

It installs `/usr/local/bin/ac-power-profile`, `/etc/systemd/system/ac-power-profile.service` (enabled, runs
once at boot) and `/etc/udev/rules.d/90-ac-power-profile.rules` (starts that service on any charger change).
Safe to re-run.

## Check it

```sh
ac-power-profile --dry-run                  # "would set performance" on the charger, "power-saver" on battery
powerprofilesctl get                        # unplug, wait 5 s, run again: the profile follows
journalctl -u ac-power-profile -n 5         # one line per plug event
bash servers/tools/ac-power-profile/test.sh # offline: fake chargers and host-heat state
```

The script waits 3 seconds before it reads the charger: USB-C chargers report "offline" for a moment right
after you plug in.

## Undo

```sh
sudo servers/tools/ac-power-profile/install.sh --undo
```

The profile stays where it was; `powerprofilesctl set balanced` resets it. To pause it without removing it:
`sudo systemctl mask ac-power-profile.service` (unmask to resume).

## Notes

- A plug or unplug during a host-heat cool-down is skipped. The next charger change, a reboot, or
  `sudo systemctl start ac-power-profile` puts it right.
- `performance` on a thin laptop runs warmer. If raven gets hot on the charger, change `performance` to
  `balanced` in `/usr/local/bin/ac-power-profile` (and in this folder, so a re-run keeps it).
