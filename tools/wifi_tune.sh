#!/bin/bash
# wifi_tune.sh — make the wireless link fit for real-time input.
#
# WiFi power save parks the radio between beacons. For a browser that costs
# you nothing; for a flight stick it means the Xbox's poll can sit waiting
# 100-200ms for the radio to wake. That dwarfs everything else in this
# project — the whole input path costs well under a millisecond — so it is
# the single thing most worth turning off.
#
#   sudo ./wifi_tune.sh            tune now, and every boot
#   sudo ./wifi_tune.sh --once     tune now, don't install the service
#   sudo ./wifi_tune.sh --status   just report what the link is doing
#
# Safe to run on a wired-only board: it finds no wireless interface and exits
# without touching anything.
set -u

MODE="${1:-install}"
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
UNIT=/etc/systemd/system/hotas-wifi-tune.service

find_iface() {
    # Anything with a wireless directory in sysfs is a WiFi interface.
    for d in /sys/class/net/*/wireless; do
        [ -e "$d" ] || continue
        basename "$(dirname "$d")"
        return 0
    done
    return 1
}

report() {
    local i="$1"
    echo "  interface : $i"
    if command -v iw >/dev/null 2>&1; then
        local ps
        ps=$(iw dev "$i" get power_save 2>/dev/null | awk '{print $NF}')
        echo "  power save: ${ps:-unknown}"
        # Band matters nearly as much: 2.4GHz shares the air with everything.
        local freq
        freq=$(iw dev "$i" link 2>/dev/null | awk '/freq:/{print $2}')
        if [ -n "${freq:-}" ]; then
            if [ "$freq" -ge 5000 ] 2>/dev/null; then
                echo "  band      : ${freq} MHz (5 GHz — good)"
            else
                echo "  band      : ${freq} MHz (2.4 GHz — prefer 5 GHz if your"
                echo "              router offers it and the Xbox can reach it)"
            fi
        fi
        iw dev "$i" link 2>/dev/null | awk '/signal:/{print "  signal    :",$2,$3}'
        iw dev "$i" link 2>/dev/null | awk '/tx bitrate:/{print "  tx rate   :",$3,$4}'
    else
        echo "  (install iw for details:  sudo apt install iw)"
    fi
}

IFACE="$(find_iface || true)"
if [ -z "${IFACE:-}" ]; then
    echo "wifi_tune: no wireless interface — nothing to do (wired is better anyway)."
    exit 0
fi

if [ "$MODE" = "--status" ]; then
    echo "=== WiFi link ==="
    report "$IFACE"
    exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "wifi_tune: run with sudo to change anything."
    report "$IFACE"
    exit 1
fi

echo "=== Tuning $IFACE for low latency ==="

# 1. Turn power save off now. This does not survive a reconnect on its own,
#    which is what the other two steps are for.
if command -v iw >/dev/null 2>&1; then
    iw dev "$IFACE" set power_save off 2>/dev/null \
        && echo "  power save off (now)" \
        || echo "  ! could not set power_save via iw"
else
    echo "  ! iw not installed; skipping the immediate change"
fi

# 2. Make it stick per-connection, so NetworkManager doesn't turn it back on
#    when the link drops and comes back mid-match. 2 = disable.
if command -v nmcli >/dev/null 2>&1; then
    changed=0
    while IFS=: read -r name type; do
        [ "$type" = "802-11-wireless" ] || continue
        nmcli connection modify "$name" 802-11-wireless.powersave 2 >/dev/null 2>&1 \
            && { echo "  power save off (NetworkManager: $name)"; changed=1; }
    done < <(nmcli -t -f NAME,TYPE connection show 2>/dev/null)
    [ "$changed" = 0 ] && echo "  (no NetworkManager wifi profiles found)"
fi

# 3. Re-apply on every boot. NetworkManager covers its own profiles; this
#    covers everything else, including systemd-networkd and wpa_supplicant.
if [ "$MODE" != "--once" ]; then
    cat > "$UNIT" <<EOF
[Unit]
Description=HOTAS Bridge - disable WiFi power save for low-latency input
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$SELF --once

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable hotas-wifi-tune.service >/dev/null 2>&1 \
        && echo "  installed hotas-wifi-tune.service (re-applies on boot)"
fi

echo ""
report "$IFACE"
echo ""
echo "Still seeing lag? In rough order of what actually helps:"
echo "  - Put the Pi on 5 GHz, on a channel your neighbours aren't using."
echo "  - Better still, plug the Pi into the router with a cable. The Xbox"
echo "    can stay wireless; removing one of the two radios is most of the win."
echo "  - Give the Pi a static lease so a DHCP renewal can't land mid-match."
echo "  - Watch the throttle screen: it now reads real ms, then 1.4s, then"
echo "    STALL as the link degrades, so you can see changes take effect."
