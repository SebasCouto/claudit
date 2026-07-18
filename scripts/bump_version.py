#!/usr/bin/env python3
# bump_version.py — mantiene sincronizada la version del plugin claudit en sus dos
# manifests y la incrementa. Fuente unica de la version: plugin.json; marketplace.json
# la espeja. Sin dependencias (usa el python3 que el plugin ya requiere).
#
#   python3 scripts/bump_version.py                     # patch +1 (default)
#   python3 scripts/bump_version.py minor               # minor +1, patch=0
#   python3 scripts/bump_version.py major               # major +1, minor=patch=0
#   python3 scripts/bump_version.py --if-plugin-changed # solo bumpea si hay cambios
#                                                       # staged bajo plugin/claude-code/
#                                                       # (lo invoca el hook pre-commit)
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(subprocess.check_output(
    ["git", "rev-parse", "--show-toplevel"], text=True).strip())
PLUGIN = ROOT / "plugin" / "claude-code" / ".claude-plugin" / "plugin.json"
MARKET = ROOT / ".claude-plugin" / "marketplace.json"


def staged():
    out = subprocess.check_output(["git", "diff", "--cached", "--name-only"], text=True)
    return [l for l in out.splitlines() if l]


def escribir(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")


def subir(actual, parte):
    ma, mi, pa = (int(x) for x in actual.split("."))
    if parte == "major":
        return f"{ma + 1}.0.0"
    if parte == "minor":
        return f"{ma}.{mi + 1}.0"
    return f"{ma}.{mi}.{pa + 1}"


def main():
    args = sys.argv[1:]
    if "--if-plugin-changed" in args:
        if not any(f.startswith("plugin/claude-code/") for f in staged()):
            return  # commit sin cambios del plugin -> no se toca la version
        args = [a for a in args if a != "--if-plugin-changed"]
    parte = args[0] if args and args[0] in ("major", "minor", "patch") else "patch"

    plugin = json.loads(PLUGIN.read_text("utf-8"))
    actual = plugin["version"]
    nueva = subir(actual, parte)

    plugin["version"] = nueva
    escribir(PLUGIN, plugin)

    market = json.loads(MARKET.read_text("utf-8"))
    for p in market.get("plugins", []):
        if p.get("name") == "claudit":
            p["version"] = nueva
    escribir(MARKET, market)

    subprocess.run(["git", "add", str(PLUGIN), str(MARKET)], check=True)
    print(f"claudit: version {actual} -> {nueva} ({parte})")


if __name__ == "__main__":
    main()
