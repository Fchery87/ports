#!/usr/bin/env bash
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app="$here/portkill.py"
dest="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

chmod +x "$app"
mkdir -p "$dest"

# Desktop Entry spec: quote the arg, backslash-escape " ` $ \ inside it, then
# escape every backslash once more because Exec is also a string value.
quoted="$(printf '%s' "$app" | sed -e 's/[\\"`$]/\\&/g' -e 's/\\/\\\\/g')"
while IFS= read -r line; do
  [[ $line == Exec=* ]] && line="Exec=\"$quoted\""
  printf '%s\n' "$line"
done < "$here/portkill.desktop" > "$dest/portkill.desktop"

command -v update-desktop-database >/dev/null && update-desktop-database "$dest" || true
echo "Installed $dest/portkill.desktop"
