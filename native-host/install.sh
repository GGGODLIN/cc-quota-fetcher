#!/usr/bin/env bash
# Install the native messaging host manifest into the macOS Chrome path.
# Usage: ./install.sh <extension-id-1> [<extension-id-2>]
#
# Reads manifest.template.json, fills in path + extension IDs,
# writes to ~/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.cc.quota_writer.json
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <extension-id-1> [<extension-id-2>]" >&2
  exit 1
fi

PROJ_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$PROJ_DIR/native-host/manifest.template.json"
SOURCE_RECEIVER="$PROJ_DIR/native-host/quota-receiver.py"

# Deploy receiver outside ~/Desktop because macOS TCC blocks Chrome from spawning
# native host processes located in protected directories (Desktop/Documents/Downloads).
DEPLOY_DIR="$HOME/.claude/scripts/cc-quota-fetcher-host"
DEPLOYED_RECEIVER="$DEPLOY_DIR/quota-receiver.py"

DEST_DIR="$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"
DEST="$DEST_DIR/com.cc.quota_writer.json"

[[ -f "$TEMPLATE" ]] || { echo "no template at $TEMPLATE" >&2; exit 2; }
[[ -f "$SOURCE_RECEIVER" ]] || { echo "no receiver at $SOURCE_RECEIVER" >&2; exit 2; }

mkdir -p "$DEPLOY_DIR" "$DEST_DIR"
cp "$SOURCE_RECEIVER" "$DEPLOYED_RECEIVER"
chmod +x "$DEPLOYED_RECEIVER"

EXT1="$1"
EXT2="${2:-$EXT1}"

sed -e "s|__RECEIVER_PATH__|$DEPLOYED_RECEIVER|" \
    -e "s|__EXTENSION_ID_1__|$EXT1|" \
    -e "s|__EXTENSION_ID_2__|$EXT2|" \
    "$TEMPLATE" > "$DEST"

echo "manifest:  $DEST"
echo "receiver:  $DEPLOYED_RECEIVER (deployed from $SOURCE_RECEIVER)"
echo "ext ids:   $EXT1${2:+, $EXT2}"
