# Lessons: things that went wrong once

Each of these cost real hours on a real fleet of old laptops. The scripts here already handle most of them.

## Installing

- **Debian auto-install on Wi-Fi: use `priority=high`, not `critical`.** At `critical` the installer skips the
  Wi-Fi passphrase question and dead-ends on "passphrase too long or too short".
- **Some HP firmware boots the USB stick forever** after a finished install: it puts "USB" first in the boot
  order or ignores Debian's entry. Fix on the box with `sudo efibootmgr` (look at `BootOrder`, then
  `sudo efibootmgr -o <debian>,<others>`), and install GRUB to the removable fallback path too
  (`grub-installer/force-efi-extra-removable` in the preseed; `sudo grub-install --force-extra-removable` after).
- **Make the installer refuse the wrong machine.** A guard on the model name and on "exactly one internal disk"
  turns a USB stick that wipes disks into one that powers off harmlessly anywhere else. Power off when done,
  so a stick left in cannot reinstall on the next boot.
- **"No disk found" on Dell/HP:** BIOS SATA mode is RAID (Intel RST). Set AHCI.
- `systemctl reboot --firmware-setup` is ignored by some firmware. Use the key at power-on.
- Test the hands-off install in a VM first (`qemu` with `-smbios type=1,product=...` to fake the model).

## Networking

- **Test liveness with TCP port 22, never ping.** Many machines and firewalls drop ICMP; a "dead" box may be fine.
  `nc -zv host 22` or `tailscale ping host`.
- **Wi-Fi power saving** on Intel laptop cards makes a server crawl (slow LAN pings) and breaks `.local`
  names. Turn it off at every boot (`iw dev <if> set power_save off`; setup.sh installs a unit).
- **`name.local` often resolves to an IPv6 link-local address.** A firewall that allows SSH only from your IPv4
  LAN then refuses it. Allow `fe80::/10` too.
- **Load a default-drop firewall as a trial, every time**, not only when `SSH_CONNECTION` is set (sudo drops it):
  runtime rules only, a timed rollback armed first, and the rules file written only after a NEW session confirmed
  it gets in. Allow the ports sshd really listens on (`sshd -T`) and the address you are connected from.
- **Debian's stock nftables.conf starts with `flush ruleset`.** Reloading it wipes Docker's and Tailscale's
  rules too. Keep your rules in your own table and replace only that.
- **Docker-published ports bypass the host firewall.** Bind them to `127.0.0.1` (or the tailnet address) and share
  with `tailscale serve`. Without that, SMB, SSH or Ollama can end up open to every device on the LAN.
- **Tailscale traffic bypasses host firewalls.** Isolation between tailnet devices lives in the Tailscale policy
  file (tags, grants), not in ufw/nftables.
- **A hand-written `/etc/resolv.conf` pointing only at Tailscale's MagicDNS resolver broke all public DNS**
  when the tailnet had no global nameservers. `tailscale set --accept-dns=false` does not rewrite a static file.
- **Samba with `bind interfaces only = yes` never listens on the Tailscale interface.** Use `no` plus a firewall.
- Services that need DNS at boot can fail "temporary failure in name resolution": order them
  `After=network-online.target` and let the timer retry.

## Tailscale

- **"Policy does not permit you to SSH as user X" also means "user X does not exist".** Check the user name on the
  box before rewriting the policy.
- **Run interactive logins in a background unit,** not your SSH session. A login waiting in a session that
  died (or a box that lost power) is gone; the approval lands on nothing.
- **Disable key expiry on servers** (or tag them). Otherwise they drop off the tailnet after 180 days.
- **A service bound to the tailnet IP that starts before Tailscale has its address** sits "active" with no
  listener until someone restarts it. Bind to `0.0.0.0` plus a firewall, or wait for the address in `ExecStartPre`.
- **Tailscale SSH ignores `authorized_keys`,** including `command=` restrictions. Machine keys that must be
  limited to one command go through OpenSSH on another port.
- SSH rules in the policy file do not accept raw IPs or CIDRs as sources; use tags, groups or autogroups.

## Power and heat

- **Never run local AI models on a thin laptop.** Sustained load overheated one to a hard power-off.
- **Wake-on-LAN needs three things:** BIOS WoL on, ErP/deep-sleep off, and the OS arming the port
  (`ethtool -s <if> wol g`, at every boot). It cannot revive a box that is on but hung, nor one that lost power
  completely unless the BIOS has "power on after AC loss". Prove it once with the box really off.
- **"Restore on AC power loss" off** keeps a server down after a short outage until someone presses the button.
- **Wake once, then stop.** A watchdog that keeps sending wake packets or rebooting can fight a box that was shut
  down on purpose or for heat. Send one packet per outage; mark deliberate shutdowns "hold".
- **A laptop's own firmware writes can bite:** on one HP, writing `/sys/firmware/acpi/platform_profile` once made
  the file unreadable until reboot. host-heat leaves it alone.
- **NVIDIA can report "[GPU requires reset]" instead of a temperature.** Parse defensively, or the heat watch dies
  exactly when it is needed.
- Polling a sleeping NVIDIA GPU (`nvidia-smi`) wakes it and costs power. Check its runtime state first.
- A battery held at 100% around the clock can swell. Charge limit in the BIOS, or "fan always on", and a monthly look.
- A dead CMOS coin cell means a wrong clock at every boot until NTP syncs.
- Do not launch a graphical session on a headless GPU box for testing; it can take the box off the
  network until it is power-cycled.

## Containers (media stack)

- **Recreating gluetun orphans everything sharing its network.** The containers show "Up" with no network.
  Always `docker compose up -d --force-recreate transmission prowlarr flaresolverr` after; `port-sync.sh` checks.
- **Containers inside gluetun cannot resolve compose service names.** Give their targets fixed IPs and allow that
  subnet with `FIREWALL_OUTBOUND_SUBNETS`.
- **VPN port forwarding on, tunnel up, nothing passes:** with forwarding on, gluetun picks only servers that
  support it, and some keys or plans are refused there. Try it off, or a key made for such a server.
- **`docker compose -f path/compose.yaml` reads `.env` from the compose file's folder,** not where you stand. A
  stop command "did nothing" for that reason.
- Seerr 2.x (Jellyseerr) cannot sign in to Jellyfin 12; use 3.x.
- A copied file leaves every protection of the box it came from. Scan it where it lands.

## Working on servers

- **Verify a service right after installing it** (`systemctl is-active`, `journalctl -u`). acpid once failed to
  start silently and the lid trick did nothing.
- **Do not assume other boxes have your laptop's files and paths.** Copy the specific script you need.
- **Keep backup keys append-only.** A backup the source can delete dies with the source.
- **Make every scheduled job stoppable:** a HALT file it checks first, plus a documented `systemctl disable`.
- **Write the order of steps (what depends on what) before the plan.** A migration plan once wiped a disk one
  step before copying its data elsewhere.
- **When explaining a disk problem, name the exact disk and say whether it is physical damage or a filesystem
  flag.** "Your drive has errors" caused a panic about the wrong (healthy) drive.
- `sudo mount` instead of `udisksctl` over SSH (no desktop session to ask for permission).
- Give rsync a destination it can write (`--chmod=D755,F644`) or it fails with `mkstemp` errors.
