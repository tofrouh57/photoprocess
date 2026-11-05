# Debian 13 KDE Development VM on Proxmox --- Setup and Recovery Notes

This document records the setup and troubleshooting steps for a Debian
13 KDE virtual machine hosted on Proxmox and accessed from a MacBook
using Windows App (RDP) and SSH.

> Replace example values such as `120`, `192.168.1.120`, and `youruser`
> with the actual VM ID, IP address, and username.

## 1. VM configuration in Proxmox

Recommended starting configuration:

-   **Guest OS:** Debian 13 amd64 (x86-64), not ARM64
-   **Machine:** q35
-   **BIOS:** SeaBIOS or OVMF
-   **SCSI controller:** VirtIO SCSI single
-   **Disk:** about 64 GB, using VirtIO SCSI
-   **CPU:** host, 4 cores
-   **Memory:** 8 GB (ballooning optional; e.g. minimum 4 GB)
-   **Network:** VirtIO, attached to `vmbr0`
-   **QEMU Guest Agent:** enabled in the VM's Proxmox Options

Enable the QEMU Guest Agent from the Proxmox host (replace `120` with
the VM ID):

``` bash
qm set 120 --agent enabled=1
qm config 120 | grep agent
```

Then fully shut down and start the VM. Inside Debian:

``` bash
sudo apt install -y qemu-guest-agent
sudo systemctl enable --now qemu-guest-agent
sudo systemctl status qemu-guest-agent
```

If the agent reports a missing virtio port, verify that the agent is
enabled in Proxmox and perform a full VM shutdown/start (not just a
reboot).

## 2. Debian installation

Install Debian 13 amd64. In the software selection screen, select:

-   KDE Plasma
-   Standard system utilities

Create a normal user during installation. Use this account for everyday
work; avoid using root for normal desktop and development activity.

## 3. Give the normal user sudo access

Log in as `root` (locally, over the working RDP session, or another root
session). Add the normal user to Debian's `sudo` group:

``` bash
usermod -aG sudo youruser
id youruser
```

The `id` output should include the `sudo` group.

Completely log out of the normal user's SSH and desktop sessions, then
log in again. Test:

``` bash
sudo whoami
```

Expected output:

``` text
root
```

If the user still gets `user is not in the sudoers file`, check as root:

``` bash
getent group sudo
id youruser
grep -E '^[^#].*sudo' /etc/sudoers
```

Do not edit `/etc/sudoers` manually unless necessary. Debian normally
grants sudo through membership of the `sudo` group.

## 4. Install and enable SSH

Install the SSH server:

``` bash
sudo apt update
sudo apt install -y openssh-server
```

Enable and start it:

``` bash
sudo systemctl enable --now ssh
sudo systemctl status ssh
```

Find the VM's IP address:

``` bash
hostname -I
```

From macOS Terminal, connect using:

``` bash
ssh youruser@192.168.1.120
```

Replace the example username and IP with the actual values. Accept the
host key on first connection, then enter the Debian user's password.

If UFW is installed and active, allow SSH:

``` bash
sudo ufw allow OpenSSH
sudo ufw status
```

Keep SSH restricted to the trusted LAN; do not expose port 22 to the
internet without a deliberate security setup.

### If APT cannot locate `openssh-server`

This usually means the package indexes have not been downloaded or
Debian repositories are missing/unreachable. Inspect the repository
configuration:

``` bash
cat /etc/apt/sources.list
ls -l /etc/apt/sources.list.d/
cat /etc/apt/sources.list.d/debian.sources
```

Refresh the package indexes and inspect any errors:

``` bash
sudo apt clean
sudo apt update
```

Check DNS/network connectivity:

``` bash
ping -c 3 deb.debian.org
```

Resolve APT repository or network errors before retrying:

``` bash
sudo apt install openssh-server
```

Do not add arbitrary third-party repositories to fix this.

## 5. Install XRDP for remote KDE desktop access

Install XRDP and its Xorg backend:

``` bash
sudo apt update
sudo apt install -y xrdp xorgxrdp dbus-x11
sudo apt install -y plasma-workspace
```

Create the user's XRDP session startup file. Run this as the normal
desktop user, not as root:

``` bash
cat > ~/.xsession <<'EOF'
#!/bin/sh
exec dbus-run-session -- startplasma-x11
EOF

chmod +x ~/.xsession
```

Enable and start XRDP:

``` bash
sudo systemctl enable --now xrdp
sudo systemctl status xrdp
```

Find the VM IP:

``` bash
hostname -I
```

In Windows App on macOS, create a PC connection to the Debian VM's IP
address, using RDP (normally TCP port 3389). Sign in with the normal
Debian username and password.

## 6. Troubleshoot a black screen after the XRDP splash screen

If root can log in through RDP but the normal user gets a black screen,
XRDP itself is probably functioning. Investigate the normal user's KDE
session and home-directory configuration.

Run these checks as the affected user:

``` bash
ls -l /usr/bin/startplasma-x11
dpkg -l | grep -E 'plasma-workspace|xrdp|xorgxrdp'
cat ~/.xsession
cat ~/.xsession-errors
```

Check XRDP session-manager logs:

``` bash
sudo journalctl -u xrdp-sesman -b --no-pager -n 60
sudo journalctl -u xrdp -b --no-pager -n 60
```

Check service status:

``` bash
sudo systemctl status xrdp xrdp-sesman --no-pager
```

If the X11 startup executable is missing, inspect installed KDE packages
and Debian package availability before installing additional packages.
Depending on the Debian KDE packaging, an X11 session component may need
to be installed.

If home-directory ownership is wrong, correct only the affected user's
relevant directories. From a root session, substituting the actual
username:

``` bash
USER_NAME="youruser"
HOME_DIR="/home/$USER_NAME"

chown -R "$USER_NAME:$USER_NAME" "$HOME_DIR/.cache" "$HOME_DIR/.config" 2>/dev/null
chown "$USER_NAME:$USER_NAME" "$HOME_DIR/.xsession" 2>/dev/null
```

Do not recursively change ownership of the entire home directory unless
you have confirmed that it is necessary.

After making a change, restart XRDP:

``` bash
sudo systemctl restart xrdp-sesman
sudo systemctl restart xrdp
```

Disconnect the RDP client completely and reconnect.

## 7. Configure the French MacBook keyboard in Debian/XRDP

For a French MacBook with an Apple Aluminium ISO keyboard, the intended
Debian keyboard settings are:

-   **Keyboard model:** Apple Aluminium (ISO)
-   **Country:** France
-   **Layout:** French (Macintosh)

Configure them from Debian:

``` bash
sudo dpkg-reconfigure keyboard-configuration
```

Choose the model, country, and layout above. The French XKB variant is
named `mac`.

Check that Debian provides the variant:

``` bash
localectl list-x11-keymap-variants fr
```

The list should include:

``` text
mac
```

### Apply the layout in the XRDP desktop session

Run this inside a terminal opened in the KDE desktop reached through
RDP:

``` bash
setxkbmap -layout fr -variant mac
setxkbmap -query
```

Expected query output includes:

``` text
layout:  fr
variant: mac
```

To apply it whenever the user's X session starts, create `~/.xsessionrc`
as the normal user:

``` bash
cat > ~/.xsessionrc <<'EOF'
setxkbmap -layout fr -variant mac
EOF
```

Then disconnect and reconnect the RDP session.

**Important:** `setxkbmap` needs access to the graphical X display. If
run from an ordinary SSH shell, it may fail with:

``` text
Cannot open display "default display"
```

That does not necessarily mean the keyboard layout is invalid. Run it
inside the active XRDP KDE desktop instead.

### Essential Windows App setting: Scan Code

The key issue encountered in this setup was that Windows App's normal
keyboard translation did not match the MacBook's physical French
keyboard. Debian's `fr(mac)` and `fr(azerty)` variants both produced
incorrect characters for the top-left key.

**The working fix was to change the keyboard mode in the Windows App RDP
connection settings to `Scan Code`.**

Keep these settings together:

-   Windows App keyboard mode: **Scan Code**
-   Debian keyboard model: **Apple Aluminium (ISO)**
-   Debian XKB layout: **French Macintosh** (`fr`, variant `mac`)

After changing the Windows App setting, disconnect and reconnect the RDP
session. Test the top-left key and other special characters, including
`@`, `#`, `€`, `|`, `\\`, and `~`.

## 8. Useful diagnostic commands

``` bash
# VM network address
hostname -I
ip -br addr

# SSH
sudo systemctl status ssh
sudo journalctl -u ssh -b --no-pager -n 50

# XRDP
sudo systemctl status xrdp xrdp-sesman --no-pager
sudo journalctl -u xrdp -u xrdp-sesman -b --no-pager -n 100

# KDE X11 session
ls -l /usr/bin/startplasma-x11
cat ~/.xsession
cat ~/.xsession-errors

# User and groups
whoami
id
groups

# Keyboard (run inside the XRDP desktop)
echo "$DISPLAY"
setxkbmap -query
```

## 9. Recovery checklist

If the VM needs to be rebuilt or RDP/SSH access needs to be restored,
work through this order:

1.  Confirm the VM boots and has a LAN IP address.
2.  Confirm the normal user can log in locally or through the Proxmox
    console.
3.  Confirm the normal user belongs to the `sudo` group; log out and
    back in after adding them.
4.  Install and enable `openssh-server`; verify SSH from the Mac.
5.  Install `xrdp`, `xorgxrdp`, `dbus-x11`, and the KDE Plasma
    workspace.
6.  Confirm `/usr/bin/startplasma-x11` exists.
7.  Create the normal user's `~/.xsession` with the `dbus-run-session`
    command shown above.
8.  Enable and start XRDP.
9.  Set Debian's keyboard model to Apple Aluminium ISO and layout to
    French (Macintosh).
10. Set Windows App keyboard mode to **Scan Code**.
11. Test RDP login as the normal user, then test keyboard characters.

Keep a separate backup of important development files and configuration.
This document records the configuration and troubleshooting steps; it is
not a substitute for backing up the VM.



reconnecting a rdp session requires to execute the reconnect script to restore keyboard settings:
Debian xrdp has a dedicated reconnect script:
/etc/xrdp/reconnectwm.sh
Keep the existing content and add this at the end:
```# Restore French Mac keyboard after RDP reconnect
(
    sleep 2
    /usr/bin/setxkbmap -model pc104 -layout fr -variant mac
) &
```