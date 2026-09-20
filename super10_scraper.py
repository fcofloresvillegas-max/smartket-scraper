#!/usr/bin/env python3
"""Extrae ofertas de Super10.cl desde el JSON inicial de la página.

La página /ofertas entrega un arreglo completo de ofertas en __NEXT_DATA__;
por eso este script no necesita recorrer las páginas ni hacer clic en categorías.

Uso:
    pip install requests beautifulsoup4
    python super10_scraper.py --output-dir datos
    python super10_scraper.py --categoria lacteos --output-dir datos
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import unicodedata
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

URL = "https://www.super10.cl/ofertas"
USER_AGENT = "Super10OfferReader/1.0"
NOMBRE_SUPERMERCADO = "Super 10"
WEIGHT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|kilos?|g|gr|gramos)\b", re.IGNORECASE)
VOLUME_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(lt|l|litros?|ml|cc)\b", re.IGNORECASE)

_OFFERS_CACHE: list[dict[str, object]] | None = None


def sin_tildes(texto: str) -> str:
    normalizado = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in normalizado if not unicodedata.combining(c))


def parse_weight_kg(name: str | None) -> float | None:
    if not name:
        return None
    match = WEIGHT_RE.search(name)
    if not match:
        return None
    value = float(match.group(1).replace(",", "."))
    unit = match.group(2).lower()
    return value if unit.startswith("k") else value / 1000


def parse_volume_l(name: str | None) -> float | None:
    if not name:
        return None
    match = VOLUME_RE.search(name)
    if not match:
        return None
    value = float(match.group(1).replace(",", "."))
    unit = match.group(2).lower()
    return value if unit.startswith("l") else value / 1000


def check_robots_allowed(url: str, user_agent: str = "*") -> bool:
    """Revisa robots.txt de super10.cl antes de consultar la URL dada."""
    rp = urllib.robotparser.RobotFileParser()
    robots_url = urljoin(url, "/robots.txt")
    try:
        resp = requests.get(robots_url, timeout=20)
        resp.raise_for_status()
        rp.parse(resp.text.splitlines())
    except Exception as exc:
        print(f"[aviso] No se pudo leer robots.txt ({exc}); se aborta por precaucion.")
        return False
    return rp.can_fetch(user_agent, url)


def clean(value: object) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def money(value: object) -> int | None:
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None


def money_unitario(value: object) -> int | None:
    """Como money(), pero entiende el formato de Super10 para packs tipo
    '6x $2.000' (6 unidades por $2.000 EN TOTAL, no $2.000 cada una).
    Sin ese manejo especial, los digitos del '6' y del '$2.000' quedaban
    pegados como un solo numero ($62.000), inflando el precio 10-30 veces.
    """
    if value is None:
        return None
    text = str(value)
    bulk = re.match(r"^\s*(\d+)\s*x\s*\$?\s*([\d.,]+)", text, re.IGNORECASE)
    if bulk:
        cantidad = int(bulk.group(1))
        total = money(bulk.group(2))
        if cantidad > 0 and total is not None:
            return round(total / cantidad)
        return total
    return money(text)


def load_offers() -> list[dict[str, object]]:
    global _OFFERS_CACHE
    if _OFFERS_CACHE is not None:
        return _OFFERS_CACHE

    if not check_robots_allowed(URL):
        raise RuntimeError("robots.txt de super10.cl no permite esta ruta.")

    response = requests.get(
        URL,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "es-CL,es;q=0.9"},
        timeout=30,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    node = soup.select_one("#__NEXT_DATA__")
    if not node:
        raise RuntimeError("No se encontró __NEXT_DATA__; Super 10 pudo cambiar su estructura.")
    data = json.loads(node.string or node.get_text())
    try:
        offers = data["props"]["pageProps"]["page"]["productos"]["offers"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("No se encontró el arreglo de ofertas en el JSON de Super 10.") from exc
    if not isinstance(offers, list):
        raise RuntimeError("El arreglo de ofertas tiene un formato inesperado.")

    _OFFERS_CACHE = [x for x in offers if isinstance(x, dict)]
    return _OFFERS_CACHE


def scrape(term: str) -> dict[str, object]:
    """Filtra las ofertas ya cargadas (una sola vez por corrida) por relevancia
    con el termino, igual criterio que los demas scrapers: el nombre debe
    empezar con la primera palabra del termino buscado.
    """
    offers = load_offers()
    extracted_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    query_palabras = [w for w in sin_tildes(term.strip().lower()).split() if w]

    productos: list[dict[str, object]] = []
    for offer in offers:
        nombre = clean(offer.get("name"))
        if not nombre:
            continue
        nombre_norm = sin_tildes(nombre.lower())
        if query_palabras and not nombre_norm.startswith(query_palabras[0]):
            continue

        precio_anterior = money_unitario(offer.get("price"))
        precio_actual = money_unitario(offer.get("offer-price")) or precio_anterior
        if precio_anterior is not None and precio_actual is not None and precio_anterior <= precio_actual:
            precio_anterior = None

        weight_kg = parse_weight_kg(nombre)
        volume_l = parse_volume_l(nombre)
        unit_price = None
        unit_label = None
        if precio_actual is not None and weight_kg:
            unit_price = round(precio_actual / weight_kg)
            unit_label = f"${unit_price} x kg"
        elif precio_actual is not None and volume_l:
            unit_price = round(precio_actual / volume_l)
            unit_label = f"${unit_price} x lt"

        productos.append({
            "nombre": nombre,
            "marca": None,
            "precio_actual": precio_actual,
            "precio_anterior": precio_anterior,
            "precio_unitario": unit_price,
            "texto_precio_unitario": unit_label,
            "en_oferta": True,  # Super 10 solo publica lo que esta en oferta
            "id_producto": clean(offer.get("ean")),
            "url_producto": None,
            "url_imagen": None,
        })

    productos.sort(key=lambda p: (p["precio_actual"] is None, p["precio_actual"] or 0, p["nombre"]))
    return {
        "fuente": URL,
        "url_consultada": URL,
        "termino_busqueda": term,
        "fecha_consulta": extracted_at,
        "cantidad_productos": len(productos),
        "productos": productos,
    }


def subir_a_supabase(result: dict[str, object]) -> int:
    """Inserta los productos encontrados en la tabla precios_productos."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError(
            "Faltan las variables de entorno SUPABASE_URL y/o "
            "SUPABASE_SERVICE_ROLE_KEY. Definelas antes de correr con --supabase."
        )

    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }

    resp = requests.get(
        f"{url}/rest/v1/supermercados",
        headers=headers,
        params={"nombre": f"eq.{NOMBRE_SUPERMERCADO}", "select": "id"},
        timeout=30,
    )
    resp.raise_for_status()
    filas = resp.json()
    if filas:
        supermercado_id = filas[0]["id"]
    else:
        crear = requests.post(
            f"{url}/rest/v1/supermercados",
            headers={**headers, "Prefer": "return=representation"},
            json={"nombre": NOMBRE_SUPERMERCADO, "sitio_web": "https://www.super10.cl"},
            timeout=30,
        )
        crear.raise_for_status()
        supermercado_id = crear.json()[0]["id"]

    filas_a_insertar = []
    for p in result["productos"]:
        filas_a_insertar.append({
            "supermercado_id": supermercado_id,
            "termino_busqueda": result["termino_busqueda"],
            "id_producto_tienda": p.get("id_producto"),
            "nombre": p["nombre"],
            "marca": p.get("marca"),
            "precio_actual": p.get("precio_actual"),
            "precio_anterior": p.get("precio_anterior"),
            "precio_unitario": p.get("precio_unitario"),
            "unidad": p.get("texto_precio_unitario"),
            "en_oferta": p.get("en_oferta", False),
            "calificacion": None,
            "url_producto": p.get("url_producto"),
            "url_imagen": p.get("url_imagen"),
            "fecha_consulta": result["fecha_consulta"],
        })

    if not filas_a_insertar:
        return 0

    total_insertado = 0
    tamano_lote = 200
    for i in range(0, len(filas_a_insertar), tamano_lote):
        lote = filas_a_insertar[i : i + tamano_lote]
        resp = requests.post(
            f"{url}/rest/v1/precios_productos",
            headers=headers,
            json=lote,
            timeout=60,
        )
        resp.raise_for_status()
        total_insertado += len(lote)

    return total_insertado


def normalize(offers: list[dict[str, object]], category: str | None) -> list[dict[str, object]]:
    wanted = category.casefold().strip() if category else None
    result = []
    for offer in offers:
        category_value = clean(offer.get("category"))
        if wanted and (not category_value or wanted not in category_value.casefold()):
            continue
        result.append({
            "ean": clean(offer.get("ean")),
            "nombre": clean(offer.get("name")),
            "descripcion": clean(offer.get("description")),
            "categoria": category_value,
            "precio_anterior": money(offer.get("price")),
            "precio_oferta": money(offer.get("offer-price")),
            "precio_anterior_texto": clean(offer.get("price")),
            "precio_oferta_texto": clean(offer.get("offer-price")),
            "vigencia": clean(offer.get("date")),
            "fuente": URL,
        })
    result.sort(key=lambda x: (x["precio_oferta"] is None, x["precio_oferta"] or 0, x["nombre"] or ""))
    return result


def save(products: list[dict[str, object]], category: str | None, output_dir: Path, total_source: int) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = re.sub(r"[^a-zA-Z0-9_-]+", "_", category.strip()) if category else "todas"
    suffix = suffix.strip("_").lower() or "todas"
    result = {
        "fuente": URL,
        "fecha_consulta": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "categoria_filtro": category,
        "cantidad_ofertas_en_json": total_source,
        "cantidad_productos": len(products),
        "productos": products,
    }
    json_path = output_dir / f"super10_ofertas_{suffix}.json"
    csv_path = output_dir / f"super10_ofertas_{suffix}.csv"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = list(products[0].keys()) if products else [
        "ean", "nombre", "descripcion", "categoria", "precio_anterior", "precio_oferta",
        "precio_anterior_texto", "precio_oferta_texto", "vigencia", "fuente",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(products)
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Extrae ofertas de Super10 desde __NEXT_DATA__")
    parser.add_argument("--categoria", help="Filtro opcional, por ejemplo: lacteos, despensa o bebidas")
    parser.add_argument("--termino", help="Prueba el filtro por termino (igual al que usan los demas scrapers), ej: leche")
    parser.add_argument("--output-dir", default=".", help="Carpeta de salida")
    parser.add_argument(
        "--supabase", action="store_true",
        help="Con --termino: ademas de mostrar el resultado, sube a Supabase",
    )
    args = parser.parse_args()

    if args.termino:
        try:
            result = scrape(args.termino)
        except Exception as exc:
            print(f"Error durante la extracción: {exc}", file=sys.stderr)
            return 1
        print(f"Productos encontrados para '{args.termino}': {result['cantidad_productos']}")
        for p in result["productos"][:10]:
            print(f"  ${p['precio_actual']} - {p['nombre']}" + (f" ({p['texto_precio_unitario']})" if p.get("texto_precio_unitario") else ""))
        if args.supabase:
            try:
                insertados = subir_a_supabase(result)
                print(f"Supabase: {insertados} filas insertadas en precios_productos.")
            except Exception as exc:
                print(f"Error subiendo a Supabase: {exc}", file=sys.stderr)
                return 1
        return 0

    try:
        source = load_offers()
        products = normalize(source, args.categoria)
        json_path, csv_path = save(products, args.categoria, Path(args.output_dir), len(source))
    except Exception as exc:
        print(f"Error durante la extracción: {exc}", file=sys.stderr)
        return 1
    print(f"Ofertas en JSON inicial: {len(source)}")
    print(f"Productos después del filtro: {len(products)}")
    print(f"JSON: {json_path.resolve()}")
    print(f"CSV:  {csv_path.resolve()}")
    print("Aviso: precios y vigencias pueden cambiar; algunas ofertas requieren Club 10/RUT según el sitio.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
