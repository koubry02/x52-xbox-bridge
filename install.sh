#!/bin/bash
# install.sh — install HOTAS Bridge and configure autostart for your role.
#
# Run on each board ONCE after copying the hotas-bridge folder to /opt/:
#
#   sudo ./install.sh oberon          # single Pi, Oberon mode (recommended)
#   sudo ./install.sh sender          # OPi A, USB proxy mode
#   sudo ./install.sh receiver        # OPi B, USB proxy mode
#
# After install, services start on boot automatically.
# Manage with:  systemctl status hotas-*
#               journalctl -u hotas-oberon -f
#               systemctl restart hotas-sender
set -e

ROLE="${1:-}"
SRC="$(cd "$(dirname "$0")" && pwd)"
DST="/opt/hotas-bridge"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run with sudo"; exit 1
fi
if [ -z "$ROLE" ]; then
    echo "Usage: sudo $0 {oberon|sender|receiver}"; exit 1
fi

echo "=== Installing HOTAS Bridge to $DST ==="

# Layouts you create or edit on the board live in $DST/layouts. Back them up
# before syncing, and protect them from --delete, so an upgrade can never eat
# a profile you built in the web editor. (Layouts SHIPPED with this repo are
# still refreshed — your edits to those are in the backup tarball.)
if [ -d "$DST/layouts" ]; then
    BACKUP="$DST/layouts-backup-$(date +%Y%m%d-%H%M%S).tgz"
    tar czf "$BACKUP" -C "$DST" layouts 2>/dev/null \
        && echo "  existing layouts backed up to $BACKUP"
fi
rsync -a --delete --filter='protect layouts/**' "$SRC"/ "$DST"/ 2>/dev/null \
    || cp -r "$SRC"/. "$DST"/
chmod +x "$DST"/proxy/*.sh "$DST"/tools/*.sh 2>/dev/null || true
# Ensure every shell script is 777 on the installed copy
find "$DST" -name "*.sh" -type f -exec chmod 777 {} \; 2>/dev/null || true
chmod +x "$DST"/oberon/oberon_server.py \
         "$DST"/sender/hotas_sender.py \
         "$DST"/receiver/hotas_receiver.py 2>/dev/null || true

svc_install() {   # $1 = service filename, found anywhere in $DST
    local f
    f=$(find "$DST" -name "$1" | head -1)
    if [ -z "$f" ]; then echo "ERROR: $1 not found in $DST"; exit 1; fi
    cp "$f" /etc/systemd/system/"$1"
    echo "  installed /etc/systemd/system/$1"
}

case "$ROLE" in
# ─────────────────────────────────────────────────────────────────────────────
  oberon)
    echo "=== Mode: Oberon (single Pi) ==="
    echo "--- Installing Python deps ---"
    apt-get update -qq
    apt-get install -y -qq python3-evdev iw

    # websockets: prefer apt package (noble has python3-websockets),
    # fall back to pip if the apt version is too old (need >= 10)
    if apt-cache show python3-websockets 2>/dev/null | grep -q "Version: 1[0-9]"; then
        apt-get install -y -qq python3-websockets
    else
        pip3 install --break-system-packages "websockets>=12"
    fi

    # ---- Optional: X52 Pro MFD status display (libx52) ----
    # Provides the `x52cli` binary the server uses to write the throttle screen
    # (Pi IP, Oberon connected, ping, menu mode). Entirely optional: if this
    # fails or is skipped, the bridge runs exactly the same without the display.
    if [ "${MFD:-ask}" = "no" ]; then
        echo "--- Skipping MFD display (MFD=no) ---"
    elif command -v x52cli >/dev/null 2>&1; then
        echo "--- MFD display: x52cli already installed ---"
    else
        echo "--- Optional: X52 Pro throttle display (libx52) ---"
        DO_MFD="${MFD:-}"
        if [ -z "$DO_MFD" ]; then
            read -r -p "    Install libx52 to show status on the throttle screen? [Y/n] " ans || ans="n"
            case "$ans" in [Nn]*) DO_MFD="no";; *) DO_MFD="yes";; esac
        fi
        if [ "$DO_MFD" = "yes" ]; then
            # Ubuntu can use the maintainer's PPA. Armbian/Debian and arm64
            # generally have NO package there, so build from source instead.
            . /etc/os-release 2>/dev/null || true
            IS_UBUNTU="no"; [ "${ID:-}" = "ubuntu" ] && IS_UBUNTU="yes"

            INSTALLED="no"
            if [ "$IS_UBUNTU" = "yes" ]; then
                echo "    Ubuntu detected — trying the libx52 PPA..."
                if apt-add-repository -y ppa:nirenjan/libx52 2>/dev/null; then
                    apt-get update -qq || true
                fi
                if apt-get install -y -qq x52pro-linux 2>/dev/null \
                   || apt-get install -y -qq libx52-1 2>/dev/null \
                   || apt-get install -y -qq libx52 2>/dev/null; then
                    INSTALLED="yes"
                fi
            fi

            if [ "$INSTALLED" != "yes" ] || ! command -v x52cli >/dev/null 2>&1; then
                echo "    Building libx52 from source (Armbian/Debian/arm64 path)..."
                if [ -x "$DST/oberon/build_libx52.sh" ]; then
                    bash "$DST/oberon/build_libx52.sh" || {
                        echo "    Source build failed. The bridge will run fine"
                        echo "    without the display. See oberon/build_libx52.sh."
                    }
                else
                    echo "    build_libx52.sh missing; skipping MFD."
                fi
            fi

            if command -v x52cli >/dev/null 2>&1; then
                echo "    libx52 ready — the throttle screen will show status."
            fi
        fi
    fi

    # ---- WiFi latency ----
    # Power save parks the radio between beacons, which costs a browser
    # nothing and costs a flight stick 100-200ms. Everything else in this
    # project runs in well under a millisecond, so this is the one knob that
    # actually decides how the link feels.
    echo "--- Tuning the WiFi link ---"
    bash "$DST/tools/wifi_tune.sh" || echo "  (skipped; run tools/wifi_tune.sh by hand)"

    svc_install hotas-oberon.service
    systemctl daemon-reload
    systemctl enable --now hotas-oberon.service

    IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    [ -z "$IP" ] && IP="<this-board-ip>"

    echo ""
    echo "=== Oberon mode installed and running ==="
    echo "Game layouts : $DST/layouts  ($(ls "$DST"/layouts/*.json 2>/dev/null | wc -l) installed)"
    echo "Switch games : press the layout button on the throttle (T2 / BTN_BASE4)"
    echo "               — the active game shows on the throttle screen."
    echo "Web app      : http://$IP:8088   (status + layout editor, LAN only)"
    echo "Link check   : sudo $DST/tools/wifi_tune.sh --status"
    echo ""
    echo "Next step on Xbox: open Oberon Remote, enter $IP, press Connect."
    echo "Check status: journalctl -u hotas-oberon -f"
    ;;

# ─────────────────────────────────────────────────────────────────────────────
  sender)
    echo "=== Mode: UDP Sender (OPi A, proxy mode) ==="
    apt-get update -qq
    apt-get install -y -qq python3-evdev

    svc_install hotas-sender.service
    systemctl daemon-reload
    systemctl enable --now hotas-sender.service

    echo ""
    echo "=== Sender installed and running ==="
    echo "Check status: systemctl status hotas-sender"
    echo "Edit config:  $DST/sender/sender_config.json  then  systemctl restart hotas-sender"
    ;;

# ─────────────────────────────────────────────────────────────────────────────
  receiver)
    echo "=== Mode: USB Proxy Receiver (OPi B, proxy mode) ==="
    apt-get update -qq
    apt-get install -y -qq \
        python3 build-essential git pkg-config \
        libjsoncpp-dev liblua5.4-dev

    # Warn if luajit dev headers are present (would be selected over lua5.4)
    if pkg-config --exists luajit 2>/dev/null; then
        echo "WARNING: libluajit-*-dev is installed and will be preferred over lua5.4."
        echo "Remove it first:  sudo apt remove libluajit-*-dev"
        echo "Then re-run this installer."
        exit 1
    fi

    # Build usb-proxy if not already built
    if [ ! -x "$DST/proxy/usb-proxy/usb-proxy" ]; then
        echo "--- Building usb-proxy ---"
        (cd "$DST/proxy" && ./setup_opi_b.sh)
    else
        echo "--- usb-proxy already built, skipping ---"
    fi

    svc_install hotas-receiver.service
    svc_install hotas-proxy.service
    systemctl daemon-reload
    systemctl enable --now hotas-receiver.service

    echo ""
    echo "=== Receiver installed ==="
    echo "The RECEIVER service is running and will start on boot."
    echo "The PROXY service is installed but NOT enabled yet."
    echo ""
    echo "Complete Phase 1 (passthrough test) first:"
    echo "  cd $DST/proxy && sudo ./run_proxy.sh"
    echo ""
    echo "Once the passthrough test passes (30+ min session), enable the proxy:"
    echo "  sudo systemctl enable --now hotas-proxy"
    ;;

  *)
    echo "Unknown role '$ROLE'. Use: oberon | sender | receiver"
    exit 1
    ;;
esac
