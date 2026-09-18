#!/usr/bin/env python3
"""Extrae productos y precios del catálogo público de Lider.cl.

Lider publica los resultados de búsqueda en super.lider.cl/v/<termino> y
expone un ItemList JSON-LD con nombre, precio, disponibilidad, imagen y URL.

Uso:
    python lider_scraper.py arroz
    python lider_scraper.py "arroz integral" --output-dir datos

Requisitos:
    pip install requests beautifulsoup4
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
from typing import Any
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://super.lider.cl/v/{query}"
WEIGHT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|kilos?|g|gr|gramos)\b", re.IGNORECASE)
NOMBRE_SUPERMERCADO = "Lider"


def sin_tildes(texto: str) -> str:
    """Quita tildes para comparar texto sin que 'cafe' != 'café' cause problemas."""
    normalizado = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in normalizado if not unicodedata.combining(c))


def parse_weight_kg(name: str | None) -> float | None:
    """Extrae el peso del nombre del producto (ej: '1 kg', '400 g') en kilos."""
    if not name:
        return None
    match = WEIGHT_RE.search(name)
    if not match:
        return None
    value = float(match.group(1).replace(",", "."))
    unit = match.group(2).lower()
    return value if unit.startswith("k") else value / 1000


def check_robots_allowed(url: str, user_agent: str = "*") -> bool:
    """Revisa robots.txt de super.lider.cl antes de scrapear la URL dada.

    Usa requests (via certifi) en vez de urllib.robotparser solo, porque en
    algunos Python de Windows el almacen de certificados por defecto de
    urllib esta incompleto y falla la verificacion SSL en ciertos sitios.
    """
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


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    value = re.sub(r"\s+", " ", str(value)).strip()
    return value or None


def money_to_int(value: Any) -> int | None:
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None


def infer_brand(name: str | None) -> str | None:
    if not name:
        return None
    # Heurística conservadora para los nombres que terminan en marca.
    common = [
        "Tucapel", "Banquete", "Miraflores", "Lider", "La Romana",
        "Cuisine & Co", "Carozzi", "Jumbo", "Kellogg's", "Nestlé",
    ]
    lower = name.lower()
    for brand in common:
        if brand.lower() in lower:
            return brand
    return None


def parse_product(item: dict[str, Any]) -> dict[str, Any] | None:
    product = item.get("item", item)
    if not isinstance(product, dict):
        return None
    name = clean_text(product.get("name"))
    if not name:
        return None
    offers = product.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = money_to_int(offers.get("price") if isinstance(offers, dict) else None)
    url = clean_text(product.get("url"))
    image = product.get("image")
    if isinstance(image, list):
        image = image[0] if image else None

    weight_kg = parse_weight_kg(name)
    unit_price = round(price / weight_kg) if price and weight_kg else None

    return {
        "nombre": name,
        "marca": infer_brand(name),
        "precio_actual": price,
        "precio_anterior": None,
        "precio_unitario": unit_price,
        "texto_precio_unitario": (f"${unit_price} x kg" if unit_price else None),
        "en_oferta": False,
        "calificacion": None,
        "url_producto": url,
        "url_imagen": clean_text(image),
        "id_producto": url.rstrip("/").split("/")[-1].split("?")[0] if url else None,
        "disponibilidad": clean_text(offers.get("availability")) if isinstance(offers, dict) else None,
    }


def extract_items(soup: BeautifulSoup) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        blocks = data if isinstance(data, list) else [data]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("@type") == "ItemList":
                elements = block.get("itemListElement", [])
                if isinstance(elements, list):
                    for element in elements:
                        item = parse_product(element)
                        if item:
                            results.append(item)
    return results


def scrape(query: str, pause: float = 1.0) -> dict[str, Any]:
    del pause  # Se conserva el argumento para mantener compatible el uso anterior.
    slug = quote(query.strip().lower().replace(" ", "-"), safe="-_")
    url = BASE_URL.format(query=slug)

    if not check_robots_allowed(url):
        raise RuntimeError("robots.txt de super.lider.cl no permite esta ruta.")

    extracted_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    response = requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
            "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        timeout=60,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    query_palabras = [w for w in sin_tildes(query.strip().lower()).split() if w]
    products: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in extract_items(soup):
        nombre_norm = sin_tildes((item["nombre"] or "").lower())
        if query_palabras and not nombre_norm.startswith(query_palabras[0]):
            continue
        key = item["url_producto"] or f'{item["nombre"]}|{item["precio_actual"]}'
        if key not in seen:
            seen.add(key)
            products.append(item)
    products.sort(key=lambda p: (p["precio_actual"] is None, p["precio_actual"] or 0, p["nombre"]))
    return {
        "fuente": "super.lider.cl",
        "url_consultada": url,
        "termino_busqueda": query,
        "fecha_consulta": extracted_at,
        "cantidad_productos": len(products),
        "productos": products,
    }


def subir_a_supabase(result: dict[str, Any]) -> int:
    """Inserta los productos encontrados en la tabla precios_productos.

    Devuelve la cantidad de filas insertadas. Lanza un error claro si
    faltan las variables de entorno o si Supabase rechaza la solicitud.
    """
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
            json={"nombre": NOMBRE_SUPERMERCADO, "sitio_web": "https://www.lider.cl"},
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
            "calificacion": p.get("calificacion"),
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


def save_outputs(result: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", result["termino_busqueda"].strip()).strip("_") or "busqueda"
    json_path = output_dir / f"lider_{slug}.json"
    csv_path = output_dir / f"lider_{slug}.csv"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = [
        "nombre", "marca", "precio_actual", "precio_anterior", "precio_unitario",
        "texto_precio_unitario", "en_oferta", "calificacion", "url_producto",
        "url_imagen", "id_producto", "disponibilidad",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result["productos"])
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Extrae productos y precios públicos de Lider.cl")
    parser.add_argument("query", help="Término de búsqueda, por ejemplo: arroz")
    parser.add_argument("--max-pages", type=int, default=1, help="Conservado por compatibilidad; Lider entrega el ItemList actual completo")
    parser.add_argument("--pause", type=float, default=1.0, help="Conservado por compatibilidad")
    parser.add_argument("--output-dir", default=".", help="Carpeta de salida")
    parser.add_argument(
        "--supabase", action="store_true",
        help="Ademas de guardar JSON/CSV local, sube los resultados a Supabase",
    )
    args = parser.parse_args()
    if args.max_pages < 1 or args.pause < 0:
        parser.error("--max-pages debe ser >= 1 y --pause debe ser >= 0")
    try:
        result = scrape(args.query, args.pause)
        json_path, csv_path = save_outputs(result, Path(args.output_dir))
    except requests.RequestException as exc:
        print(f"Error de conexión con Lider: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error durante la extracción: {exc}", file=sys.stderr)
        return 1
    print(f"Productos encontrados: {result['cantidad_productos']}")
    print(f"JSON: {json_path.resolve()}")
    print(f"CSV:  {csv_path.resolve()}")

    if args.supabase:
        try:
            insertados = subir_a_supabase(result)
            print(f"Supabase: {insertados} filas insertadas en precios_productos.")
        except Exception as exc:
            print(f"Error subiendo a Supabase: {exc}", file=sys.stderr)
            return 1

    print("Aviso: los precios pueden variar por ubicación, disponibilidad y promociones.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
