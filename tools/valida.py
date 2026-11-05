#!/usr/bin/env python3
"""Valida v1/stations.json, v1/config.json i la coherència d'anuncis i font del temps. Surt amb codi 1 si hi ha errors."""
import base64
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


def llegeix_sobre(nom):
    """Contingut d'un sobre signat de v1/, o None si no existeix. La signatura la comprova qui el fa servir."""
    cami = os.path.join(ROOT, "v1", nom)
    if not os.path.exists(cami):
        return None
    with open(cami, encoding="utf-8") as f:
        return json.loads(base64.b64decode(json.load(f)["payloadB64"]))


def anuncis_i_temps():
    """Amb anuncis visibles, el temps ha de venir de MET Norway (Open-Meteo gratuït és només no comercial)."""
    try:
        anuncis = llegeix_sobre("anuncis.json")
    except (ValueError, KeyError, TypeError) as e:
        return [f"anuncis.json il·legible: {e}"]
    if anuncis is None:
        return []
    formats = anuncis.get("formats") or {}
    if not ("show" in formats.values() or (anuncis.get("command") == "show" and not formats)):
        return []
    try:
        seccions = llegeix_sobre("seccions.json")
    except (ValueError, KeyError, TypeError) as e:
        return [f"seccions.json il·legible: {e}"]
    font = seccions.get("weatherSource") if seccions else None
    if font != "met-norway":
        return [f"anuncis.json mostra anuncis, però la font del temps de seccions.json és {font or 'cap'}: "
                "ha de ser met-norway (Open-Meteo és només per a ús no comercial)"]
    return []


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
        badge = s.get("badge")
        if badge is not None and not (isinstance(badge, str) and 1 <= len(badge.strip()) <= 12):
            avisos.append(f"{nom}: 'badge' ha de tenir d'1 a 12 caràcters (l'app no el mostrarà)")
        color = s.get("badgeColor")
        if color is not None and not re.fullmatch(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", str(color)):
            avisos.append(f"{nom}: 'badgeColor' ha de ser #RRGGBB o #AARRGGBB")
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
        for k, v in (cfg.get("features") or {}).items():
            if not isinstance(v, bool):
                errors.append(f"config.json: 'features.{k}' ha de ser true o false")
    except json.JSONDecodeError as e:
        errors.append(f"config.json no és JSON vàlid: {e}")
    errors += anuncis_i_temps()
    for a in avisos:
        print("⚠️ ", a)
    for e in errors:
        print("❌", e)
    print(f"{len(vistos)} emissores, {len(errors)} errors, {len(avisos)} avisos")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
