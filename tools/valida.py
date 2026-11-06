#!/usr/bin/env python3
"""Valida v1/stations.json, v1/config.json, les seves signatures i la coherència d'anuncis i font del temps. Surt amb codi 1 si hi ha errors."""
import base64
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TERRITORIS = {"catalunya", "pais-valencia", "illes-balears", "andorra", "catalunya-nord", "franja", "alguer"}
CATEGORIES = {"generalista", "informativa", "musical", "local"}
TIPUS = {"hls", "aac", "mp3"}
ID = re.compile(r"^[a-z0-9_]+$")
CLAU_PUBLICA = base64.b64decode("k2NM7b10E5o6mmKn8RBNLSlL9f1uey4Ebn3Sq/fqEew=")
SIGNATS = ("stations.json", "config.json")

# Verificació Ed25519 (RFC 8032, 6), sense dependències: és lenta, però només es fa servir aquí, amb dades públiques.
_P = 2**255 - 19
_Q = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _suma(a, b):
    x, y = (a[1] - a[0]) * (b[1] - b[0]) % _P, (a[1] + a[0]) * (b[1] + b[0]) % _P
    c, d = 2 * a[3] * b[3] * _D % _P, 2 * a[2] * b[2] % _P
    e, f, g, h = y - x, d - c, d + c, y + x
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _producte(n, punt):
    r = (0, 1, 1, 0)
    while n:
        if n & 1:
            r = _suma(r, punt)
        punt, n = _suma(punt, punt), n >> 1
    return r


def _punt(b):
    """Descomprimeix un punt de 32 bytes; None si no és vàlid."""
    y = int.from_bytes(b, "little")
    signe, y = y >> 255, y & (2**255 - 1)
    if y >= _P:
        return None
    xx = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P:
        x = x * _I % _P
    if (x * x - xx) % _P or (x == 0 and signe):
        return None
    if x & 1 != signe:
        x = _P - x
    return (x, y, 1, x * y % _P)


_BASE = _punt((4 * pow(5, _P - 2, _P) % _P).to_bytes(32, "little"))


def ed25519_valida(clau, missatge, firma):
    if len(firma) != 64:
        return False
    a, r, s = _punt(clau), _punt(firma[:32]), int.from_bytes(firma[32:], "little")
    if a is None or r is None or s >= _Q:
        return False
    h = int.from_bytes(hashlib.sha512(firma[:32] + clau + missatge).digest(), "little") % _Q
    e, d = _producte(s, _BASE), _suma(r, _producte(h, a))
    return (e[0] * d[2] - d[0] * e[2]) % _P == 0 and (e[1] * d[2] - d[1] * e[2]) % _P == 0


def signatures():
    """Les versions 35 i posteriors no accepten cap canvi d'aquests fitxers sense una signatura vàlida (R-03)."""
    errors = []
    for nom in SIGNATS:
        cami = os.path.join(ROOT, "v1", nom)
        if not os.path.exists(cami + ".sig"):
            errors.append(f"falta v1/{nom}.sig: cal signar-lo")
            continue
        with open(cami, "rb") as f:
            dades = f.read()
        with open(cami + ".sig", encoding="ascii", errors="replace") as f:
            try:
                firma = base64.b64decode(f.read().strip(), validate=True)
            except ValueError:
                firma = b""
        if not ed25519_valida(CLAU_PUBLICA, b"dialec-dades-v1\n" + nom.encode() + b"\n" + dades, firma):
            errors.append(f"v1/{nom}.sig no quadra amb el fitxer: cal tornar a signar-lo")
    return errors


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
    errors += signatures()
    for a in avisos:
        print("⚠️ ", a)
    for e in errors:
        print("❌", e)
    print(f"{len(vistos)} emissores, {len(errors)} errors, {len(avisos)} avisos")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
