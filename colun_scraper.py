#!/usr/bin/env python3
"""Extrae precios de productos de Mi Tienda Colun usando su sitemap público.

No consulta /catalogsearch/. Lee el sitemap declarado en robots.txt y visita
solo fichas de producto .html, que están permitidas por las reglas publicadas.

Uso:
    pip install requests beautifulsoup4
    python colun_sitemap_scraper.py leche --output-dir datos
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
import urllib.robotparser
import warnings
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from urllib3.exceptions import InsecureRequestWarning

warnings.filterwarnings("ignore", category=InsecureRequestWarning)

SITE = "https://www.mitiendacolun.cl"
SITEMAP_URL = f"{SITE}/media/sitemapb2c/sitemap.xml"
USER_AGENT = "ColunPriceResearch/1.0 (contactar al administrador del sitio)"
PRODUCT_EXCLUDE_PARTS = ("/catalog/", "/descuentos/", "/customer/", "/checkout/")
NOMBRE_SUPERMERCADO = "Colun"
WEIGHT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|kilos?|g|gr|gramos)\b", re.IGNORECASE)
VOLUME_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(lt|l|litros?|ml|cc)\b", re.IGNORECASE)


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
    """Revisa robots.txt de mitiendacolun.cl antes de consultar la URL dada."""
    rp = urllib.robotparser.RobotFileParser()
    robots_url = urljoin(url, "/robots.txt")
    try:
        resp = requests.get(robots_url, timeout=20, verify=False)
        resp.raise_for_status()
        rp.parse(resp.text.splitlines())
    except Exception as exc:
        print(f"[aviso] No se pudo leer robots.txt ({exc}); se aborta por precaucion.")
        return False
    return rp.can_fetch(user_agent, url)


def clean(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"\s+", " ", value).strip() or None


def money(value: str | None) -> int | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    return int(digits) if digits else None


def get_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "es-CL,es;q=0.9"})
    # El entorno de ejecución puede carecer de un certificado intermedio de
    # Colun; se consulta únicamente contenido público HTTPS.
    session.verify = False
    return session


def sitemap_product_urls(session: requests.Session, term: str) -> list[str]:
    response = session.get(SITEMAP_URL, timeout=30)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    term_norm = term.casefold()
    urls: list[str] = []
    for node in root.findall("s:url/s:loc", ns):
        url = clean(node.text)
        if not url or not url.lower().endswith(".html"):
            continue
        path = urlparse(url).path.lower()
        if any(part in path for part in PRODUCT_EXCLUDE_PARTS):
            continue
        # El sitemap incluye categorías y productos. Las categorías conocidas
        # se omiten; las fichas se filtran después por el nombre real.
        if path.rstrip("/").split("/")[-1] in {
            "leche.html", "postres.html", "cremas.html", "manjar.html",
            "aguas-y-jugos.html", "queso.html", "mantequilla.html",
            "yoghurt.html", "blancas.html", "blancas-sin-lactosa.html",
            "sabores.html", "sabor-sin-lactosa.html", "cultivada.html",
            "leche-en-polvo.html",
        }:
            continue
        if term_norm in path or not term_norm:
            urls.append(url)
    return list(dict.fromkeys(urls))


def parse_product(html: str, url: str, term: str) -> dict[str, object] | None:
    soup = BeautifulSoup(html, "html.parser")
    title_node = soup.select_one('.product-info-main .page-title, meta[property="og:title"]')
    if not title_node:
        return None
    name = clean(title_node.get("content") if title_node.name == "meta" else title_node.get_text(" ", strip=True))
    if not name or term.casefold() not in name.casefold():
        return None

    final_node = soup.select_one('.product-info-main [data-price-type="finalPrice"]')
    old_node = soup.select_one('.product-info-main [data-price-type="oldPrice"]')
    box = soup.select_one('.product-info-main .price-box')
    box_text = clean(box.get_text(" ", strip=True) if box else "") or ""
    current = money(final_node.get_text(" ", strip=True) if final_node else None)
    old = money(old_node.get_text(" ", strip=True) if old_node else None)
    if current is None:
        match = re.search(r"Precio especial\s*\$?\s*([\d.]+)", box_text, re.I)
        current = money(match.group(1)) if match else None
    if current is None:
        match = re.search(r"\$\s*([\d.]+)", box_text)
        current = money(match.group(1)) if match else None
    if old is None:
        match = re.search(r"Precio habitual\s*\$?\s*([\d.]+)", box_text, re.I)
        old = money(match.group(1)) if match else None
    discount = None
    match = re.search(r"-\s*(\d+)\s*%", box_text)
    if match:
        discount = int(match.group(1))

    image = soup.select_one('.product-info-main img, meta[property="og:image"]')
    image_url = image.get("content") if image and image.name == "meta" else image.get("src") if image else None

    weight_kg = parse_weight_kg(name)
    volume_l = parse_volume_l(name)
    unit_price = None
    unit_label = None
    if current is not None and weight_kg:
        unit_price = round(current / weight_kg)
        unit_label = f"${unit_price} x kg"
    elif current is not None and volume_l:
        unit_price = round(current / volume_l)
        unit_label = f"${unit_price} x lt"

    return {
        "nombre": name,
        "marca": "Colun",
        "precio_actual": current,
        "precio_anterior": old if old and current and old > current else None,
        "precio_unitario": unit_price,
        "texto_precio_unitario": unit_label,
        "descuento_porcentaje": discount,
        "en_oferta": bool(discount or (old and current and old > current)),
        "url_producto": url,
        "url_imagen": image_url,
    }


def scrape(term: str, pause: float, max_products: int, session: requests.Session) -> dict[str, object]:
    if not check_robots_allowed(SITEMAP_URL):
        raise RuntimeError("robots.txt de mitiendacolun.cl no permite leer el sitemap.")

    urls = sitemap_product_urls(session, term)
    products: list[dict[str, object]] = []
    for index, url in enumerate(urls[:max_products], 1):
        try:
            response = session.get(url, timeout=30)
            response.raise_for_status()
            item = parse_product(response.text, url, term)
            if item and item["precio_actual"] is not None:
                products.append(item)
            print(f"[{index}/{min(len(urls), max_products)}] {len(products)} productos", file=sys.stderr)
        except requests.RequestException as exc:
            print(f"[aviso] No se pudo consultar {url}: {exc}", file=sys.stderr)
        if index < min(len(urls), max_products):
            time.sleep(max(0.0, pause))
    products.sort(key=lambda p: (p["precio_actual"] is None, p["precio_actual"] or 0, str(p["nombre"])))
    return {
        "fuente": SITE,
        "sitemap": SITEMAP_URL,
        "termino_busqueda": term,
        "fecha_consulta": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "urls_en_sitemap_filtradas": len(urls),
        "cantidad_productos": len(products),
        "productos": products,
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
            json={"nombre": NOMBRE_SUPERMERCADO, "sitio_web": SITE},
            timeout=30,
        )
        crear.raise_for_status()
        supermercado_id = crear.json()[0]["id"]

    filas_a_insertar = []
    for p in result["productos"]:
        filas_a_insertar.append({
            "supermercado_id": supermercado_id,
            "termino_busqueda": result["termino_busqueda"],
            "id_producto_tienda": None,
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


def save(result: dict[str, object], output_dir: Path, term: str) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", term).strip("_") or "busqueda"
    jp = output_dir / f"colun_{slug}.json"
    cp = output_dir / f"colun_{slug}.csv"
    jp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = [
        "nombre", "marca", "precio_actual", "precio_anterior", "precio_unitario",
        "texto_precio_unitario", "descuento_porcentaje", "en_oferta", "url_producto", "url_imagen",
    ]
    with cp.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result["productos"])
    return jp, cp


def main() -> int:
    parser = argparse.ArgumentParser(description="Extrae productos Colun desde sitemap permitido")
    parser.add_argument("term", help="Término para filtrar nombres, por ejemplo leche")
    parser.add_argument("--pause", type=float, default=1.5, help="Pausa entre fichas")
    parser.add_argument("--max-products", type=int, default=100, help="Máximo de fichas a consultar")
    parser.add_argument("--output-dir", default=".")
    parser.add_argument(
        "--supabase", action="store_true",
        help="Ademas de guardar JSON/CSV local, sube los resultados a Supabase",
    )
    args = parser.parse_args()
    if args.pause < 0 or args.max_products < 1:
        parser.error("--pause debe ser >= 0 y --max-products >= 1")
    try:
        result = scrape(args.term, args.pause, args.max_products, get_session())
        jp, cp = save(result, Path(args.output_dir), args.term)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Productos encontrados: {result['cantidad_productos']}")
    print(f"JSON: {jp.resolve()}")
    print(f"CSV:  {cp.resolve()}")

    if args.supabase:
        try:
            insertados = subir_a_supabase(result)
            print(f"Supabase: {insertados} filas insertadas en precios_productos.")
        except Exception as exc:
            print(f"Error subiendo a Supabase: {exc}", file=sys.stderr)
            return 1

    print("Aviso: precios y disponibilidad pueden cambiar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
