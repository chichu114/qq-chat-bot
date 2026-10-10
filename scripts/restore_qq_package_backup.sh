#!/usr/bin/env bash
set -euo pipefail

QQ_PACKAGE="/Applications/QQ.app/Contents/Resources/app/package.json"
latest_backup="$(ls -t "$QQ_PACKAGE".bak.* 2>/dev/null | head -n 1 || true)"

if [[ -z "$latest_backup" ]]; then
  echo "ERROR: no backup found for $QQ_PACKAGE"
  exit 1
fi

echo "Restoring latest backup:"
echo "$latest_backup"
echo
echo "This needs your Mac password because QQ.app is under /Applications."
sudo install -o root -g wheel -m 0644 "$latest_backup" "$QQ_PACKAGE"

echo "Restored QQ package.json from backup."
