#!/usr/bin/env bash
set -euo pipefail

QQ_APP="/Applications/QQ.app"
QQ_APP_RESOURCES="$QQ_APP/Contents/Resources/app"
QQ_PACKAGE="$QQ_APP_RESOURCES/package.json"
QQ_DOCS="$HOME/Library/Containers/com.tencent.qq/Data/Documents"
NAPCAT_DIR="$QQ_DOCS/napcat"
DOCS_LOADER="$QQ_DOCS/loadNapCat.js"
APP_LOADER="$QQ_APP_RESOURCES/loadNapCat.js"

echo "== NapCat Mac install repair =="
echo "QQ app: $QQ_APP"
echo "QQ package: $QQ_PACKAGE"
echo "NapCat dir: $NAPCAT_DIR"
echo

if [[ ! -d "$QQ_APP" ]]; then
  echo "ERROR: QQ.app not found at $QQ_APP"
  exit 1
fi

if [[ ! -f "$QQ_PACKAGE" ]]; then
  echo "ERROR: QQ package.json not found at $QQ_PACKAGE"
  exit 1
fi

if [[ ! -f "$NAPCAT_DIR/napcat.mjs" ]]; then
  echo "ERROR: NapCat files were not found at $NAPCAT_DIR"
  echo "Run NapCatInstaller first, then run this repair script again."
  exit 1
fi

echo "Step 1/4: writing user loader at $DOCS_LOADER"
cat > "$DOCS_LOADER" <<'JS'
const path = require('path');
const currentPath = path.dirname(__filename);

(async () => {
  await import('file://' + path.join(currentPath, './napcat/napcat.mjs'));
})();
JS

chmod 0644 "$DOCS_LOADER"

timestamp="$(date +%Y%m%d-%H%M%S)"
backup="$QQ_PACKAGE.bak.$timestamp"
tmp_package="$(mktemp /tmp/qq-package.XXXXXX)"
trap 'rm -f "$tmp_package"' EXIT

echo "Step 2/4: preparing patched QQ package.json"
python3 - "$QQ_PACKAGE" "$tmp_package" <<'PY'
import json
import sys

source, target = sys.argv[1:3]
with open(source, "r", encoding="utf-8") as f:
    data = json.load(f)

data["main"] = "./loadNapCat.js"

with open(target, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY

echo "Step 3/4: backing up and replacing QQ package.json"
echo "This step needs your Mac password because QQ.app is under /Applications."
sudo cp "$QQ_PACKAGE" "$backup"
sudo tee "$APP_LOADER" >/dev/null <<JS
const path = require('path');
const userLoader = '$DOCS_LOADER';

(async () => {
  await import('file://' + userLoader);
})();
JS
sudo chmod 0644 "$APP_LOADER"
sudo install -o root -g wheel -m 0644 "$tmp_package" "$QQ_PACKAGE"

echo "Step 4/4: verifying patch"
python3 - "$QQ_PACKAGE" "$APP_LOADER" <<'PY'
import json
import sys
from pathlib import Path

package_path, app_loader = sys.argv[1:3]
with open(package_path, "r", encoding="utf-8") as f:
    data = json.load(f)

actual = data.get("main")
if actual != "./loadNapCat.js":
    print("ERROR: package.json main was not updated")
    print("expected: ./loadNapCat.js")
    print("actual:  ", actual)
    raise SystemExit(1)

if not Path(app_loader).is_file():
    print("ERROR: app loader was not created")
    print("missing:", app_loader)
    raise SystemExit(1)

print("package.json main:", actual)
print("app loader:", app_loader)
PY

echo
echo "Repair complete."
echo "Backup saved at: $backup"
echo
echo "Next:"
echo "1. Quit QQ completely if it is open."
echo "2. Start QQ/NapCat from NapCatInstaller's NapCat entry, or reopen QQ if the installer has already switched the entry."
echo "3. Look for the NapCat WebUI URL in the startup log."
