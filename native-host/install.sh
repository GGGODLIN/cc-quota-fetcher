#!/usr/bin/env bash
set -eEuo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  printf 'usage: %s <extension-id-1> [<extension-id-2>]\n' "$0" >&2
  exit 1
fi

PROJ_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
NATIVE_DIR="$PROJ_DIR/native-host"
TEMPLATE="$NATIVE_DIR/manifest.template.json"
SOURCE_RECEIVER="$NATIVE_DIR/quota-receiver.py"
SOURCE_POLLER="$NATIVE_DIR/quota_poller.py"
SOURCE_KEYCHAIN="$NATIVE_DIR/keychain.py"
SOURCE_PLIST="$NATIVE_DIR/com.cc.quota-poller.plist"
SOURCE_INSTALLER="$NATIVE_DIR/install.sh"

DEPLOY_DIR="$HOME/.claude/scripts/cc-quota-fetcher-host"
DEPLOYED_RECEIVER="$DEPLOY_DIR/quota-receiver.py"
DEPLOYED_POLLER="$DEPLOY_DIR/quota_poller.py"
DEPLOYED_KEYCHAIN="$DEPLOY_DIR/keychain.py"

MANIFEST_DIR="$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"
MANIFEST_DEST="$MANIFEST_DIR/com.cc.quota_writer.json"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_DEST="$LAUNCH_AGENTS_DIR/com.cc.quota-poller.plist"
LABEL="com.cc.quota-poller"
DOMAIN="gui/$(/usr/bin/id -u)"

EXT1="$1"
EXT2="${2:-$EXT1}"
for extension_id in "$EXT1" "$EXT2"; do
  [[ "$extension_id" =~ ^[a-p]{32}$ ]] || { printf 'invalid extension id: %s\n' "$extension_id" >&2; exit 2; }
done

for source in "$TEMPLATE" "$SOURCE_RECEIVER" "$SOURCE_POLLER" "$SOURCE_KEYCHAIN" "$SOURCE_PLIST"; do
  [[ -f "$source" ]] || { printf 'missing source: %s\n' "$source" >&2; exit 2; }
done

/usr/bin/python3 -c 'import ast, pathlib, sys; [ast.parse(pathlib.Path(path).read_text()) for path in sys.argv[1:]]' "$SOURCE_RECEIVER" "$SOURCE_POLLER" "$SOURCE_KEYCHAIN"
/bin/bash -n "$SOURCE_INSTALLER"
/usr/bin/plutil -lint "$SOURCE_PLIST" >/dev/null
/usr/bin/python3 -c 'import json, sys; [json.load(open(path, encoding="utf-8")) for path in sys.argv[1:]]' "$TEMPLATE"

atomic_copy() {
  local source="$1"
  local destination="$2"
  local mode="$3"
  local temporary
  temporary="$(/usr/bin/mktemp "${destination}.tmp.XXXXXX")"
  /bin/cp "$source" "$temporary"
  /bin/chmod "$mode" "$temporary"
  /bin/mv -f "$temporary" "$destination"
}

STAGING_DIR="$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/cc-quota-fetcher-install.XXXXXX")"
STAGED_MANIFEST="$STAGING_DIR/com.cc.quota_writer.json"
STAGED_PLIST="$STAGING_DIR/com.cc.quota-poller.plist"
cleanup() {
  /bin/rm -f "$STAGING_DIR"/*
  /bin/rmdir "$STAGING_DIR"
}
trap cleanup EXIT

/usr/bin/python3 -c 'import json,sys; source,destination,receiver,ext1,ext2=sys.argv[1:]; payload=json.load(open(source,encoding="utf-8")); payload["path"]=receiver; payload["allowed_origins"]=[f"chrome-extension://{ext1}/",f"chrome-extension://{ext2}/"]; json.dump(payload,open(destination,"w",encoding="utf-8"),indent=2); open(destination,"a",encoding="utf-8").write("\n")' "$TEMPLATE" "$STAGED_MANIFEST" "$DEPLOYED_RECEIVER" "$EXT1" "$EXT2"
/usr/bin/python3 -c 'import plistlib,sys; source,destination,poller=sys.argv[1:]; payload=plistlib.load(open(source,"rb")); payload["ProgramArguments"][0]=poller; plistlib.dump(payload,open(destination,"wb"),sort_keys=False)' "$SOURCE_PLIST" "$STAGED_PLIST" "$DEPLOYED_POLLER"
/usr/bin/python3 -c 'import json, sys; [json.load(open(path, encoding="utf-8")) for path in sys.argv[1:]]' "$STAGED_MANIFEST"
/usr/bin/plutil -lint "$STAGED_PLIST" >/dev/null

mkdir -p "$DEPLOY_DIR" "$MANIFEST_DIR" "$LAUNCH_AGENTS_DIR"
DESTINATIONS=("$DEPLOYED_KEYCHAIN" "$DEPLOYED_RECEIVER" "$DEPLOYED_POLLER" "$MANIFEST_DEST" "$PLIST_DEST")
SOURCES=("$SOURCE_KEYCHAIN" "$SOURCE_RECEIVER" "$SOURCE_POLLER" "$STAGED_MANIFEST" "$STAGED_PLIST")
MODES=(600 700 700 600 600)
EXISTED=()
i=0
while (( i < ${#DESTINATIONS[@]} )); do
  if [[ -e "${DESTINATIONS[$i]}" ]]; then
    /bin/cp -p "${DESTINATIONS[$i]}" "$STAGING_DIR/backup-$i"
    EXISTED[$i]=1
  else
    EXISTED[$i]=0
  fi
  i=$((i + 1))
done

SERVICE_WAS_LOADED=0
if /bin/launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  SERVICE_WAS_LOADED=1
fi

rollback() {
  local exit_code=$?
  local destination temporary
  trap - ERR
  set +e
  /bin/launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1
  i=0
  while (( i < ${#DESTINATIONS[@]} )); do
    destination="${DESTINATIONS[$i]}"
    if [[ "${EXISTED[$i]}" == "1" ]]; then
      temporary="$(/usr/bin/mktemp "${destination}.tmp.XXXXXX")"
      /bin/cp -p "$STAGING_DIR/backup-$i" "$temporary"
      /bin/mv -f "$temporary" "$destination"
    else
      /bin/rm -f "$destination"
    fi
    i=$((i + 1))
  done
  if [[ "$SERVICE_WAS_LOADED" == "1" ]]; then
    /bin/launchctl bootstrap "$DOMAIN" "$PLIST_DEST" >/dev/null 2>&1
    /bin/launchctl kickstart -k "$DOMAIN/$LABEL" >/dev/null 2>&1
  fi
  exit "$exit_code"
}
trap rollback ERR

i=0
while (( i < ${#DESTINATIONS[@]} )); do
  atomic_copy "${SOURCES[$i]}" "${DESTINATIONS[$i]}" "${MODES[$i]}"
  i=$((i + 1))
done

if [[ "$SERVICE_WAS_LOADED" == "1" ]]; then
  /bin/launchctl bootout "$DOMAIN/$LABEL"
fi
/bin/launchctl bootstrap "$DOMAIN" "$PLIST_DEST"
/bin/launchctl kickstart -k "$DOMAIN/$LABEL"
trap - ERR

printf 'manifest: %s\n' "$MANIFEST_DEST"
printf 'receiver: %s\n' "$DEPLOYED_RECEIVER"
printf 'poller: %s\n' "$DEPLOYED_POLLER"
printf 'keychain: %s\n' "$DEPLOYED_KEYCHAIN"
printf 'launch agent: %s\n' "$PLIST_DEST"
printf 'extension ids: %s%s\n' "$EXT1" "${2:+, $EXT2}"
