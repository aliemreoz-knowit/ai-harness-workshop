#!/bin/sh
# setup-pi.sh -- provision the HIL Pi (Raspberry Pi 5, Raspberry Pi OS Lite 64-bit, Bookworm).
#
# Run ON THE PI as root, from a copy of tools/hil/pi/:
#   scp -r tools/hil/pi <user>@<pi>:/tmp/hil-pi && ssh <user>@<pi> sudo sh /tmp/hil-pi/setup-pi.sh
#
# What it does (idempotent, safe to run again):
#   1. packages: rpicam-apps-lite (rpicam-vid / rpicam-hello), nothing else
#   2. static IP 10.42.0.2/24 on eth0 for the direct cable to the station laptop
#      (NetworkManager connection "hil-direct"; no gateway, no DNS: the Pi
#      keeps Wi-Fi / DHCP for updates if it has them)
#   3. users: `tinystream` (runs the daemon, groups video+render) and the deploy
#      user HIL_USER (default `hil`, ssh key login only) with a sudoers rule for
#      exactly the commands hil.py runs
#   4. /usr/local/bin/tinystreamd-run, /etc/default/tinystreamd, the systemd unit
#      (Restart=always), /opt/tinystream for the clip; unit enabled
#   5. camera check: rpicam-hello --list-cameras, one 320x240 YUV420 frame must be
#      exactly 115200 bytes (no stride padding)
# The daemon binary itself comes from the station: hil.py run --target pi.
#
# Environment: HIL_USER (default hil), HIL_IP (default 10.42.0.2/24), HIL_IFACE (default eth0),
#              HIL_PUBKEY (an ssh public key line for HIL_USER; optional), SKIP_APT=1, SKIP_NET=1.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
HIL_USER="${HIL_USER:-hil}"
HIL_IP="${HIL_IP:-10.42.0.2/24}"
HIL_IFACE="${HIL_IFACE:-eth0}"

say() { printf '[setup-pi] %s\n' "$*"; }

[ "$(id -u)" = 0 ] || { echo "run as root (sudo sh $0)" >&2; exit 2; }
[ "$(uname -m)" = aarch64 ] || say "warning: not aarch64 ($(uname -m)); the daemon binary is a static aarch64 build"

# ---- 1. packages ---------------------------------------------------------------------------
if [ "${SKIP_APT:-0}" != 1 ]; then
    say "installing rpicam-apps-lite"
    apt-get update -q
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q rpicam-apps-lite
fi
command -v rpicam-vid >/dev/null || { echo "rpicam-vid missing" >&2; exit 1; }

# ---- 2. static IP on the direct link -------------------------------------------------------
if [ "${SKIP_NET:-0}" != 1 ]; then
    if command -v nmcli >/dev/null; then
        if nmcli -t -f NAME con show | grep -qx hil-direct; then
            nmcli con mod hil-direct ipv4.addresses "$HIL_IP"
        else
            nmcli con add type ethernet ifname "$HIL_IFACE" con-name hil-direct \
                ipv4.method manual ipv4.addresses "$HIL_IP" ipv4.never-default yes \
                ipv6.method disabled connection.autoconnect yes connection.autoconnect-priority 100
        fi
        nmcli con up hil-direct || say "hil-direct not up yet (cable unplugged?): it comes up with the cable"
    else
        # older images (dhcpcd)
        if ! grep -q "hil-direct" /etc/dhcpcd.conf 2>/dev/null; then
            printf '\n# hil-direct\ninterface %s\nstatic ip_address=%s\nnogateway\n' "$HIL_IFACE" "$HIL_IP" \
                >> /etc/dhcpcd.conf
        fi
    fi
    say "direct link: $HIL_IFACE = $HIL_IP (station laptop: 10.42.0.1/24)"
fi

# ---- 3. users and sudo -----------------------------------------------------------------------
id tinystream >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin tinystream
usermod -a -G video,render tinystream
if ! id "$HIL_USER" >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash "$HIL_USER"
    passwd -l "$HIL_USER" >/dev/null       # key login only
fi
if [ -n "${HIL_PUBKEY:-}" ]; then
    home="$(getent passwd "$HIL_USER" | cut -d: -f6)"
    install -d -m 0700 -o "$HIL_USER" -g "$HIL_USER" "$home/.ssh"
    touch "$home/.ssh/authorized_keys"
    grep -qxF "$HIL_PUBKEY" "$home/.ssh/authorized_keys" || printf '%s\n' "$HIL_PUBKEY" >> "$home/.ssh/authorized_keys"
    chown "$HIL_USER:$HIL_USER" "$home/.ssh/authorized_keys"; chmod 0600 "$home/.ssh/authorized_keys"
fi
cat > /etc/sudoers.d/tinystream-hil <<EOF
# hil.py (station laptop) deploys and drives tinystreamd; nothing else.
$HIL_USER ALL=(root) NOPASSWD: /usr/bin/install -m 0755 /tmp/tinystreamd.hil /usr/local/bin/tinystreamd
$HIL_USER ALL=(root) NOPASSWD: /usr/bin/install -m 0644 -D /tmp/clip.hil /opt/tinystream/poster_30f.y8
$HIL_USER ALL=(root) NOPASSWD: /usr/bin/tee /etc/default/tinystreamd
$HIL_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart tinystreamd
$HIL_USER ALL=(root) NOPASSWD: /usr/bin/pkill -STOP -x rpicam-vid, /usr/bin/pkill -CONT -x rpicam-vid
EOF
chmod 0440 /etc/sudoers.d/tinystream-hil
visudo -cf /etc/sudoers.d/tinystream-hil >/dev/null

# ---- 4. service ----------------------------------------------------------------------------------
install -d -m 0755 /opt/tinystream
install -m 0755 "$HERE/tinystreamd-run" /usr/local/bin/tinystreamd-run
[ -f /etc/default/tinystreamd ] || install -m 0644 "$HERE/tinystreamd.default" /etc/default/tinystreamd
install -m 0644 "$HERE/tinystreamd.service" /etc/systemd/system/tinystreamd.service
systemctl daemon-reload
systemctl enable tinystreamd >/dev/null
if [ -x /usr/local/bin/tinystreamd ]; then
    systemctl restart tinystreamd
    say "tinystreamd restarted: $(systemctl is-active tinystreamd)"
else
    say "no /usr/local/bin/tinystreamd yet: run 'hil.py run --target pi --ref <known-good ref>' on the station (CHECKLIST.md: GOOD)"
fi

# ---- 5. camera ----------------------------------------------------------------------------------
if rpicam-hello --list-cameras 2>&1 | grep -q "Available cameras"; then
    tmp="$(mktemp)"
    systemctl stop tinystreamd 2>/dev/null || true       # the camera has one user at a time
    if rpicam-vid -n --frames 1 --codec yuv420 --width 320 --height 240 -o "$tmp" >/dev/null 2>&1; then
        size="$(wc -c < "$tmp")"
        if [ "$size" -eq 115200 ]; then
            say "camera OK: one 320x240 YUV420 frame = 115200 bytes (no stride padding)"
        else
            say "WARNING: one frame is $size bytes, expected 115200: stride padding, the pipe source would tear"
        fi
    else
        say "WARNING: rpicam-vid could not capture a frame"
    fi
    rm -f "$tmp"
    [ -x /usr/local/bin/tinystreamd ] && systemctl start tinystreamd
else
    say "WARNING: no camera found (ribbon cable? 'rpicam-hello --list-cameras')"
fi
say "done"
