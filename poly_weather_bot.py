#!/usr/bin/env python3
"""
Polymarket weather-market snapshot bot.

Escanea mercados activos en Polymarket, filtra los relacionados a clima,
captura precio, volumen, liquidez y top-of-book, y appendea a un archivo
JSONL diario.

Diseñado para correr como scheduled job en GitHub Actions.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

GAMMA_API = "https://gamma-api.polymarket.com/markets"
CLOB_BOOK = "https://clob.polymarket.com/book"

# Keywords con word boundaries (\b). Match estricto contra la PREGUNTA solamente.
# Lista conservadora: preferimos perder algunos mercados de clima legítimos
# antes que llenarnos de falsos positivos (deportes, política, etc).
WEATHER_PATTERNS = [
    r"\brain\b", r"\brainfall\b", r"\bsnow\b", r"\bsnowfall\b",
    r"\btemperature\b", r"\btemperatures\b",
    r"\bhurricane\b",       # singular — "Hurricanes" (plural) cae en el equipo NHL
    r"\bweather\b",
    r"\btornado\b", r"\btornadoes\b",
    r"\bblizzard\b",
    r"\bheat wave\b", r"\bheatwave\b",
    r"\bfrost\b",
    r"\btyphoon\b", r"\bcyclone\b",
    r"\bflooding\b",        # "flood" suelto matchea "flooded market"
    r"\bdrought\b",
    r"\bwildfire\b", r"\bwildfires\b",
    r"\bfahrenheit\b", r"\bcelsius\b",
    r"\bprecipitation\b",
    r"\bnoaa\b",            # NOAA aparece en mercados que resuelven por dato oficial
    r"\bnws\b",             # National Weather Service
    r"\baccuweather\b",
]

# Excluir explícitamente mercados deportivos que disparan falso positivo.
# Por ejemplo "Carolina Hurricanes" (equipo de NHL).
EXCLUDE_PATTERNS = [
    r"\bhurricanes\b",      # equipo NHL siempre va en plural
    r"\bnhl\b", r"\bnba\b", r"\bnfl\b", r"\bmlb\b",
    r"\bstanley cup\b",
    r"\bsuper bowl\b",
]

WEATHER_RE = re.compile("|".join(WEATHER_PATTERNS), re.IGNORECASE)
EXCLUDE_RE = re.compile("|".join(EXCLUDE_PATTERNS), re.IGNORECASE)

OUT_DIR = Path("snapshots")
TIMEOUT = 20


def log(msg, level="INFO"):
    """Logueo simple con timestamp. Sale a stdout/stderr para GitHub Actions."""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    stream = sys.stderr if level == "ERROR" else sys.stdout
    print(f"[{ts}] {level:5} | {msg}", file=stream, flush=True)


def loads_or(value, default):
    """Gamma API a veces devuelve JSON encoded strings, a veces listas."""
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def is_weather_market(market):
    """Filtra estricto: solo question, con word boundaries y exclusiones."""
    question = market.get("question") or ""
    if not question:
        return False
    if EXCLUDE_RE.search(question):
        return False
    return bool(WEATHER_RE.search(question))


def fetch_active_markets():
    """Pagina por todos los mercados activos."""
    markets = []
    offset = 0
    limit = 500
    while True:
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": offset,
        }
        r = requests.get(GAMMA_API, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        markets.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
        if offset > 5000:  # safety cap
            break
    return markets


def fetch_book(token_id):
    """Top-of-book para un token (YES o NO). Devuelve top 5 de cada lado."""
    try:
        r = requests.get(
            CLOB_BOOK,
            params={"token_id": token_id},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        book = r.json()
        return {
            "bids": book.get("bids", [])[:5],
            "asks": book.get("asks", [])[:5],
        }
    except Exception as e:
        return {"error": str(e)}


def snapshot_market(market):
    token_ids = loads_or(market.get("clobTokenIds"), [])
    outcomes = loads_or(market.get("outcomes"), [])
    prices = loads_or(market.get("outcomePrices"), [])

    snap = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "id": market.get("id"),
        "slug": market.get("slug"),
        "question": market.get("question"),
        "end_date": market.get("endDate"),
        "volume": market.get("volume"),
        "volume_24h": market.get("volume24hr"),
        "liquidity": market.get("liquidity"),
        "outcomes": outcomes,
        "prices": prices,
        "books": [],
    }

    for tid in token_ids:
        snap["books"].append({"token_id": tid, **fetch_book(tid)})

    return snap


def main():
    OUT_DIR.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_file = OUT_DIR / f"{today}.jsonl"

    log("snapshot run start")

    try:
        all_markets = fetch_active_markets()
    except Exception as e:
        log(f"FATAL: no se pudieron traer mercados: {e}", level="ERROR")
        sys.exit(1)

    weather = [m for m in all_markets if is_weather_market(m)]
    log(f"{len(all_markets)} mercados activos, {len(weather)} de clima")

    written = 0
    errors = 0
    with out_file.open("a") as f:
        for m in weather:
            try:
                snap = snapshot_market(m)
                f.write(json.dumps(snap) + "\n")
                written += 1
                log(f"  OK  {(snap['question'] or '')[:80]}")
            except Exception as e:
                errors += 1
                log(
                    f"  ERR {(m.get('question') or '')[:80]} -> {e}",
                    level="ERROR",
                )

    log(f"resumen: {written} escritos, {errors} errores -> {out_file}")


if __name__ == "__main__":
    main()
