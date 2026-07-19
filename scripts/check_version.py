#!/usr/bin/env python3
# check_version.py — gate de CI para PRs que tocan el plugin. NO bumpea ni pushea nada:
# solo VALIDA y falla (exit 1) si algo no cumple. Reemplaza al bot que commiteaba el
# bump a main, para que el versionado sea 100% por PR (main queda blindado).
#
# Valida dos cosas:
#   1) los dos manifests (plugin.json y marketplace.json) tienen la MISMA version;
#   2) la version del PR es MAYOR que la de la rama base (default: origin/main).
#
#   python3 scripts/check_version.py                 # compara contra origin/main
#   python3 scripts/check_version.py origin/develop  # compara contra otra base
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(subprocess.check_output(
    ["git", "rev-parse", "--show-toplevel"], text=True).strip())
PLUGIN_REL = "plugin/claude-code/.claude-plugin/plugin.json"
PLUGIN = ROOT / PLUGIN_REL
MARKET = ROOT / ".claude-plugin" / "marketplace.json"
FIX = "Corre: python3 scripts/bump_version.py <patch|minor|major> y sumalo al PR."


def parse(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except (ValueError, TypeError):
        return None


def market_version(text):
    for p in json.loads(text).get("plugins", []):
        if p.get("name") == "claudit":
            return p.get("version")
    return None


def base_version(base_ref):
    try:
        out = subprocess.check_output(
            ["git", "show", f"{base_ref}:{PLUGIN_REL}"], text=True,
            stderr=subprocess.DEVNULL)
        return json.loads(out)["version"]
    except (subprocess.CalledProcessError, KeyError, json.JSONDecodeError):
        return None  # base sin manifest (repo nuevo) -> no se puede comparar, se omite


def main():
    base_ref = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    plugin_v = json.loads(PLUGIN.read_text("utf-8"))["version"]
    market_v = market_version(MARKET.read_text("utf-8"))
    errs = []

    if plugin_v != market_v:
        errs.append(f"manifests desincronizados: plugin.json={plugin_v} vs "
                    f"marketplace.json={market_v}. {FIX}")

    if parse(plugin_v) is None:
        errs.append(f"version invalida en plugin.json: {plugin_v!r}")

    base_v = base_version(base_ref)
    if base_v is not None and parse(plugin_v) is not None:
        if parse(plugin_v) <= parse(base_v):
            errs.append(f"la version no subio: {base_ref}={base_v}, este PR={plugin_v}. {FIX}")

    if errs:
        print("version-check FALLO:", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    print(f"version-check OK: {plugin_v} (base {base_ref}={base_v}), manifests en sync")


if __name__ == "__main__":
    main()
