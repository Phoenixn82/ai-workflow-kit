# luks-unlock: heimdall unlocks an encrypted laptop at boot

A laptop with full-disk encryption (LUKS) that reboots on its own (an update, an idle reboot, a power cut) stops at
the disk password and waits for someone at the keyboard. With this, heimdall types it for you: the boot image
starts a tiny ssh server on the LAN, heimdall's [fleet-wake](../../../framework/_tools/fleet-wake/fleet-wake) sees
the machine sitting at the password prompt, and pipes in a key of its own. Your normal disk password keeps working,
at the keyboard and over ssh.

It is optional, and it is a trade-off: read [the security note](#security-what-you-give-up) before you set it up.

## How it works

1. The machine boots. Its boot image (initramfs) brings up the wired network with a fixed LAN address and starts
   dropbear (a small ssh server) on port 22. The only thing a login there gets is the disk password prompt.
2. fleet-wake on heimdall (every 2 minutes) sees "the tailnet address does not answer, the LAN unlock address
   does": the machine is locked. It connects as `root@<NAME>_UNLOCK_HOST` with a dedicated key
   (`~/.ssh/fleet-wake-unlock`), checks the boot image's host key against a pinned list
   (`~/.config/fleet-wake/unlock_known_hosts`, `StrictHostKeyChecking=yes`), and sends `~/.config/fleet-wake/<name>.luks`
   followed by a newline.
3. That key opens its own LUKS key slot, the boot continues, and the machine comes back on the tailnet a minute or
   two later. If it is still locked one check after the try, fleet-wake sends one alert, and you unlock it by hand
   (`ssh root@<unlock address>` from a machine on the LAN, then type the password).

fleet-wake only does this for `odin` and `thor`, and only when `<NAME>_UNLOCK_HOST` is set in heimdall's
`hosts.env` and the `.luks` file exists. Without the file it never dials out.

## What you need

- A LUKS-encrypted root disk, and a **wired** network port the boot image can drive (Wi-Fi in an initramfs is
  possible but not covered here).
- A fixed LAN address for the boot image (a static address in the kernel command line, or a DHCP reservation for
  that port's MAC address). Tailscale is not running yet at that point, so heimdall must reach it over the LAN.
- heimdall with fleet-wake set up ([profiles/heimdall](../../../profiles/heimdall/README.md#waking-boxes-fleet-wake-and-wake-watch)),
  and an ssh login from this machine to heimdall as your own user (`ssh heimdall true` works without a prompt;
  `sudo` drops your ssh agent, so a key that lives only in an agent will not do; Tailscale SSH is fine).

## Step 1: ssh in the boot image

### Arch (mkinitcpio, busybox hooks)

Install `dropbear`, `mkinitcpio-dropbear`, `mkinitcpio-netconf` and `mkinitcpio-utils`. This path needs the
busybox-style hooks (`base udev ... encrypt`); if your `HOOKS` use `systemd` and `sd-encrypt`, use
mkinitcpio-systemd-tool instead (not covered here).

1. Swap the `encrypt` hook for `netconf dropbear encryptssh` with a drop-in that sorts after your distro's own
   (Omarchy writes `omarchy_hooks.conf`), so it edits the final list. `/etc/mkinitcpio.conf.d/zz_remote_unlock.conf`:

   ```bash
   # Remote LUKS unlock over ssh: encrypt -> netconf + dropbear + encryptssh. encryptssh still asks on the
   # local console too. Undo: delete this file and the ip= argument, rebuild the boot image.
   _ru=()
   for _h in "${HOOKS[@]}"; do
     if [[ $_h == encrypt ]]; then _ru+=(netconf dropbear encryptssh); else _ru+=("$_h"); fi
   done
   HOOKS=("${_ru[@]}")
   unset _ru _h
   ```

2. Network: add `ip=<address>::<gateway>:<netmask>:<hostname>:<interface>:none` to the kernel command line (Limine:
   `KERNEL_CMDLINE[default]+=" ip=..."` in `/etc/default/limine`; GRUB: `GRUB_CMDLINE_LINUX`). The interface is
   the name the boot image sees, often `eth0`. `<address>` is what goes in `<NAME>_UNLOCK_HOST`.
3. Authorized keys: `/etc/dropbear/root_key` (mode 600) holds the public keys that may log in: heimdall's
   `~/.ssh/fleet-wake-unlock.pub` (make it on heimdall: `ssh-keygen -t ed25519 -N '' -C fleet-wake-unlock -f ~/.ssh/fleet-wake-unlock`)
   and, if you like, one key of yours for unlocking by hand. Put `no-port-forwarding,no-agent-forwarding,no-X11-forwarding`
   in front of each line. The hook makes root's shell `cryptsetup_shell` and starts dropbear with password logins
   and port forwarding off, so a login gets the password prompt and nothing else.
4. Rebuild the boot image (`sudo mkinitcpio -P`, or `sudo limine-mkinitcpio` on Omarchy) and read its output: it
   prints the dropbear host key fingerprints.

The dropbear hook copies this machine's OpenSSH host keys into the boot image when they exist, and generates its
own in `/etc/dropbear/` otherwise. Either way the host key sits in the unencrypted boot partition (see the security
note).

### Debian and Ubuntu (initramfs-tools)

Not tested by the kit; the shape is the same. `sudo apt install dropbear-initramfs`; keys in
`/etc/dropbear/initramfs/authorized_keys` with
`no-port-forwarding,no-agent-forwarding,no-X11-forwarding,command="/bin/cryptroot-unlock"` in front;
`DROPBEAR_OPTIONS="-j -k -s -p 22"` in `/etc/dropbear/initramfs/dropbear.conf`; the address with `IP=` in
`/etc/initramfs-tools/initramfs.conf` (same format as above); then `sudo update-initramfs -u`. Test the hand path
before you rely on heimdall.

### Test the hand path first

Reboot, and from another machine on the LAN: `ssh root@<unlock address>`. You should get the password prompt and
nothing else; type the password and the boot continues. Only then add heimdall's key.

## Step 2: the key slot (this script)

On the machine whose disk gets unlocked, as your own user:

```sh
servers/tools/luks-unlock/luks-unlock-setup --name odin --scheduler heimdall --dry-run   # the plan
servers/tools/luks-unlock/luks-unlock-setup --name odin --scheduler heimdall --unlock-host 192.0.2.20
```

It backs up the LUKS header to `/root/luks-header-<mapping>-<date>.img`, makes a random 64-character key in RAM,
adds it as a new key slot (it asks for your current disk password once), tests the new slot exactly the way the
unlock sends it, copies it to heimdall at `~/.config/fleet-wake/<name>.luks` (mode 600) and compares checksums, and
pins the boot image's host keys in heimdall's `~/.config/fleet-wake/unlock_known_hosts` under the unlock address.
The key never touches this machine's disk. Without `--unlock-host`, it reads `<NAME>_UNLOCK_HOST` from your
`~/.config/ai-workflow-kit/hosts.env`. `--device /dev/...` picks the LUKS partition when `/` is not on it.
`luks-unlock-setup --self-test` checks its parsing offline.

The header backup holds every key slot as it was that day. Keep it somewhere safe and offline: with it and any
password that was valid then, someone can open the disk even after you change the password.

## Step 3: heimdall

In heimdall's `~/.config/ai-workflow-kit/hosts.env`: `ODIN_UNLOCK_HOST=<the boot image's LAN address>` (or
`THOR_UNLOCK_HOST`). fleet-wake already runs from its timer; nothing else to enable.

## Check it

```sh
# on heimdall
ls -l ~/.config/fleet-wake/odin.luks ~/.ssh/fleet-wake-unlock      # both there, -rw------- for the .luks
ssh-keygen -F 192.0.2.20 -f ~/.config/fleet-wake/unlock_known_hosts # the pinned boot-image key
# on the laptop
sudo cryptsetup luksDump /dev/<luks partition> | grep -A1 'Keyslots:'   # one slot more than before
```

Then reboot the laptop with nobody at the keyboard. heimdall's log (`journalctl --user -u fleet-wake -n 20`)
shows the machine going `locked`, then `up`. Expect a few minutes from power-on to a login.

## Undo

```sh
servers/tools/luks-unlock/luks-unlock-setup --undo --name odin   # removes the slot heimdall's key opens, deletes the key on heimdall
servers/tools/luks-unlock/luks-unlock-setup --undo --name odin --slot 2   # the key on heimdall is gone already: kill slot 2 (asks for a password that stays)
```

To drop remote unlock completely: delete heimdall's line from `/etc/dropbear/root_key`, remove the drop-in and the
`ip=` argument, rebuild the boot image, and remove `<NAME>_UNLOCK_HOST` and `unlock_known_hosts` on heimdall.

## Security: what you give up

Disk encryption protects a laptop that is lost or stolen while it is off. Remote unlock moves part of that
protection to heimdall:

- **heimdall now holds a key that opens this disk.** The `.luks` file lives in your home folder on heimdall, so
  anyone with root on heimdall, and anything that runs as your user there (scripts, agents, a compromised
  container with your home mounted), can read it.
- **A stolen laptop stays locked only while heimdall is not stolen too.** Someone who takes both, or takes the
  laptop and later gets into heimdall, can unlock the disk.
- **The boot image's host key is not secret.** It sits in the unencrypted boot partition. A thief who has the laptop
  and is on your home LAN could copy that key, answer at the unlock address and receive heimdall's key. The pinned
  host key stops anyone else on the LAN from doing this, not someone holding the laptop.
- The key only works on this one disk, and it is a slot of its own: your own password, and any other slot, are
  untouched.

**If the laptop is stolen or lost**, delete `~/.config/fleet-wake/<name>.luks` on heimdall at once, so nothing
answers a fake unlock prompt. **If heimdall is stolen or compromised**, kill the slot on the laptop:
`luks-unlock-setup --undo --name <name> --slot <N>` (`sudo cryptsetup luksDump` lists the slots; the script
printed the number at setup). After any of these, a new header backup is a good idea, and an old one should go.

Not worth it when the laptop rarely reboots unattended, or when it holds data you would not want on the same
footing as heimdall's.
