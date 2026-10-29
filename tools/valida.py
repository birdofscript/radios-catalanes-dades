#!/usr/bin/env python3
"""Valida v1/stations.json i v1/config.json. Surt amb codi 1 si hi ha errors."""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TERRITORIS = {"catalunya", "pais-valencia", "illes-balears", "andorra", "catalunya-nord", "franja", "alguer"}
CATEGORIES = {"generalista", "informativa", "musical", "local"}
TIPUS = {"hls", "aac", "mp3"}
ID = re.compile(r"^[a-z0-9_]+$")


def es_url(u):
    return isinstance(u, str) and (u.startswith("http://") or u.startswith("https://"))


def main():
    errors, avisos = [], []
    try:
        with open(os.path.join(ROOT, "v1", "stations.json"), encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ stations.json no és JSON vàlid: {e}")
        return 1
    if not isinstance(data.get("version"), int):
        errors.append("falta 'version' (enter)")
    vistos = set()
    for i, s in enumerate(data.get("stations", [])):
        nom = s.get("id") or f"#{i}"
        if not ID.match(s.get("id", "")):
            errors.append(f"{nom}: 'id' no vàlid")
        if s.get("id") in vistos:
            errors.append(f"{nom}: 'id' repetit")
        vistos.add(s.get("id"))
        if not s.get("name", "").strip():
            errors.append(f"{nom}: falta 'name'")
        if not es_url(s.get("streamUrl")):
            errors.append(f"{nom}: 'streamUrl' no vàlid")
        for u in s.get("fallbackUrls", []):
            if not es_url(u):
                errors.append(f"{nom}: 'fallbackUrls' no vàlid: {u}")
        if s.get("territory") not in TERRITORIS:
            avisos.append(f"{nom}: 'territory' desconegut")
        if s.get("category") not in CATEGORIES:
            avisos.append(f"{nom}: 'category' desconeguda")
        if s.get("streamType") not in TIPUS:
            avisos.append(f"{nom}: 'streamType' desconegut")
        logo = s.get("logo") or s.get("id")
        if not os.path.exists(os.path.join(ROOT, "v1", "logos", f"{logo}.webp")):
            avisos.append(f"{nom}: falta v1/logos/{logo}.webp")
    if not vistos:
        errors.append("cap emissora")
    try:
        with open(os.path.join(ROOT, "v1", "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg.get("minAppVersionCode", 0), int):
            errors.append("config.json: 'minAppVersionCode' ha de ser un enter")
    except json.JSONDecodeError as e:
        errors.append(f"config.json no és JSON vàlid: {e}")
    for a in avisos:
        print("⚠️ ", a)
    for e in errors:
        print("❌", e)
    print(f"{len(vistos)} emissores, {len(errors)} errors, {len(avisos)} avisos")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
