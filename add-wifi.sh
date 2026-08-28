#!/usr/bin/env bash
# Add (or update) a Wi-Fi network on the Pi, safely.
#
#   ./add-wifi.sh "SSID" "password" [priority]
#
# Run it ON THE PI (or via ssh). Adding a network never disturbs the
# connection you are currently using, so this is safe to run over SSH.
# Higher priority wins when several saved networks are in range.
# Ethernet always wins over Wi-Fi regardless.
set -euo pipefail

SSID="${1:-}"
PSK="${2:-}"
PRIO="${3:-10}"
HIDDEN="${4:-no}"      # pass "hidden" for a network that doesn't broadcast
                       # its SSID (e.g. a dormant iPhone hotspot)

if [[ -z "$SSID" || -z "$PSK" ]]; then
    echo "usage: $0 \"SSID\" \"password\" [priority] [hidden]" >&2
    echo "example: $0 \"MyPhoneHotspot\" \"hunter2\" 10 hidden" >&2
    echo "note: priority is relative — higher wins. Home wifi is 100." >&2
    exit 1
fi

if [[ ${#PSK} -lt 8 ]]; then
    echo "error: WPA passwords must be at least 8 characters" >&2
    exit 1
fi

# Derive a safe profile name. SSIDs may be a single space, emoji, etc., so
# fall back to a generic slug rather than producing an empty name.
# printf (not echo) so no trailing newline sneaks into the slug.
SLUG="$(printf '%s' "$SSID" | tr -c 'a-zA-Z0-9' '-' \
        | sed 's/-\{1,\}/-/g; s/^-//; s/-$//')"
[[ -z "$SLUG" ]] && SLUG="unnamed"
CON="wifi-$SLUG"

# Replace any existing profile for this SSID so re-running is idempotent.
if nmcli -g NAME connection show | grep -qx "$CON"; then
    echo "replacing existing profile '$CON'"
    sudo nmcli connection delete "$CON" >/dev/null
fi

sudo nmcli connection add type wifi con-name "$CON" ifname wlan0 ssid "$SSID" \
    wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PSK" \
    connection.autoconnect yes connection.autoconnect-priority "$PRIO" \
    >/dev/null

if [[ "$HIDDEN" == "hidden" ]]; then
    # Actively probe for this SSID instead of waiting to see it in a scan.
    sudo nmcli connection modify "$CON" 802-11-wireless.hidden yes
    echo "  (marked hidden: the Pi will actively probe for it)"
fi

echo "added '$CON' (ssid=[$SSID], priority=$PRIO)"
echo
echo "saved networks, highest priority first:"
nmcli -f NAME,TYPE,AUTOCONNECT-PRIORITY connection show \
    | grep -E 'wifi|NAME' || true
echo
echo "It will join automatically when in range. To test right now"
echo "(will drop any current Wi-Fi link, but not ethernet):"
echo "    sudo nmcli connection up $CON"
