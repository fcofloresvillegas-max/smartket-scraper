#!/usr/bin/env python3
"""Extrae productos y precios de búsquedas públicas de Santa Isabel.

Uso:
    python santaisabel_scraper.py arroz
    python santaisabel_scraper.py arroz --max-pages 3 --output-dir datos
    python santaisabel_scraper.py arroz --supabase   (ademas sube los resultados a Supabase)

Requisitos:
    pip install playwright requests
    playwright install chromium

Para subir a Supabase (opcional, con --supabase), estas dos variables de
entorno deben estar definidas:
    SUPABASE_URL              -> ej: https://zvbkcrzluztuuwvkquny.supabase.co
    SUPABASE_SERVICE_ROLE_KEY -> la llave secreta "service_role" del proyecto
                                  (Project Settings > API en el panel de Supabase)

NUNCA pongas la service_role key directo en este archivo ni la subas a
GitHub. En tu compu se pasa como variable de entorno antes de correr el
script; en GitHub Actions se guarda como Secret del repositorio.
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
from urllib.parse import quote_plus, urljoin

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE_URL = "https://www.santaisabel.cl/busqueda?ft={query}&src=Sugerencia"
PRICE_RE = re.compile(r"\$\s*([\d.]+)")
WEIGHT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|kilos?|g|gr|gramos)\b", re.IGNORECASE)
NOMBRE_SUPERMERCADO = "Santa Isabel"


def check_robots_allowed(url: str, user_agent: str = "*") -> bool:
    """Revisa robots.txt de santaisabel.cl antes de scrapear la URL dada.

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


def money_to_int(value: str | None) -> int | None:
    """Convierte '$1.790' o '1.790' en 1790."""
    if not value:
        return None
    match = PRICE_RE.search(value)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


def clean_text(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def extract_brand(card, metadata: dict[str, Any], nombre: str | None) -> str | None:
    # El campo "brand" de los metadatos a veces viene limpio, pero en algunos
    # sitios (visto en Santa Isabel) puede traer pegado todo el texto de la
    # oferta ("Oferta Lleva 2 por $1.890 ... Cuisine & Co"). Antes de confiar
    # en el, exigimos que se vea como un nombre de marca real: corto, sin
    # signos de precio.
    meta_brand = clean_text(str(metadata.get("brand"))) if metadata.get("brand") else None
    if meta_brand and len(meta_brand) < 40 and "$" not in meta_brand and meta_brand.lower() not in {"agregar", "oferta"}:
        return meta_brand
    # Fallback: Santa Isabel suele mostrar la marca como un enlace antes del título.
    # El texto accesible de ese enlace a veces viene como "Marca <nombre completo>"
    # (la marca pegada al nombre entero del producto), asi que si el candidato
    # termina exactamente en el nombre, nos quedamos solo con lo que sobra antes.
    candidates = card.locator("a").all_inner_texts()
    for text in candidates:
        text = clean_text(text)
        if not text or text.lower() in {"agregar", "oferta"} or "$" in text:
            continue
        if nombre and text.endswith(nombre):
            prefix = text[: -len(nombre)].strip()
            if prefix and prefix.lower() != nombre.lower():
                return prefix
            continue
        if nombre and text == nombre:
            continue
        if len(text) < 40:
            return text
    return None


def extract_card(card, base_url: str) -> dict[str, Any] | None:
    name = clean_text(card.get_attribute("data-cnstrc-item-name"))
    if not name:
        return None

    current = money_to_int(card.get_attribute("data-cnstrc-item-price"))
    if current is None:
        current = money_to_int(card.inner_text())

    old_price = None
    old_nodes = card.locator(".line-through").all_inner_texts()
    if old_nodes:
        old_price = money_to_int(" ".join(old_nodes))

    unit_text = clean_text(
        card.locator(".ppum-price-container").first.inner_text()
        if card.locator(".ppum-price-container").count()
        else None
    )
    unit_price = money_to_int(unit_text)

    # El fallback de arriba (leer todo el texto de la tarjeta) a veces agarra
    # un precio de combo/multi-unidad ("2 x $3.690") en vez del precio real
    # de una unidad. Si tenemos precio por kg y el peso viene en el nombre,
    # usamos eso para detectar y corregir esos casos.
    weight_kg = parse_weight_kg(name)
    unidad_es_por_kg = bool(unit_text) and "kg" in unit_text.lower()
    if unit_price and weight_kg and unidad_es_por_kg:
        expected = unit_price * weight_kg
        if current and expected > 0 and current / expected >= 1.8:
            current = round(expected)

    # Si el "precio anterior" no queda mas alto que el actual, no es una
    # oferta real - probablemente ruido de la extraccion - asi que se descarta.
    if old_price is not None and current is not None and old_price <= current:
        old_price = None

    metadata: dict[str, Any] = {}
    raw_metadata = card.get_attribute("data-gtm-impression")
    if raw_metadata:
        try:
            metadata = json.loads(raw_metadata)
        except json.JSONDecodeError:
            pass

    link = card.locator('a[href$="/p"]').first.get_attribute("href")
    image = card.locator("img").first.get_attribute("src")
    rating = None
    rating_text = clean_text(card.inner_text())
    rating_match = re.search(r"(?<![\d.])([0-5](?:[.,]\d)?)(?![\d.])", rating_text or "")
    if rating_match:
        try:
            rating = float(rating_match.group(1).replace(",", "."))
        except ValueError:
            pass

    return {
        "nombre": name,
        "marca": extract_brand(card, metadata, name),
        "precio_actual": current,
        "precio_anterior": old_price,
        "precio_unitario": unit_price,
        "texto_precio_unitario": unit_text,
        "en_oferta": old_price is not None,
        "calificacion": rating,
        "url_producto": urljoin(base_url, link) if link else None,
        "url_imagen": image,
        "id_producto": metadata.get("id") or card.get_attribute("data-cnstrc-item-id"),
    }


def dismiss_cookies(page) -> None:
    for label in ("Aceptar todas las cookies", "Aceptar", "Continuar"):
        try:
            button = page.get_by_role("button", name=re.compile(label, re.I)).first
            if button.is_visible(timeout=800):
                button.click(timeout=1500)
                return
        except Exception:
            pass


def scrape(query: str, max_pages: int = 10, pause: float = 1.2, headless: bool = True) -> dict[str, Any]:
    encoded = quote_plus(query.strip())
    base_url = BASE_URL.format(query=encoded)

    if not check_robots_allowed(base_url):
        raise RuntimeError("robots.txt de santaisabel.cl no permite esta ruta.")

    extracted_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        page = browser.new_page(locale="es-CL", viewport={"width": 1440, "height": 1000})
        try:
            products: list[dict[str, Any]] = []
            seen: set[str] = set()
            query_palabras = [w for w in sin_tildes(query.strip().lower()).split() if w]

            # Santa Isabel muestra normalmente 40 productos en page=1 y el resto en
            # page=2. El botón visual "Siguiente" puede cambiar el contenido
            # sin cambiar page.url, por eso navegamos directamente con el
            # parámetro page y no dependemos de detectar el cambio de URL.
            for page_number in range(1, max_pages + 1):
                page_url = base_url if page_number == 1 else f"{base_url}&page={page_number}"
                page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_selector("[data-cnstrc-item-name]", timeout=60000)
                dismiss_cookies(page)

                # Espera a que termine la carga dinámica de la página actual.
                page.wait_for_timeout(int(max(pause, 0.5) * 1000))
                cards = page.locator("[data-cnstrc-item-name]")
                count = cards.count()
                if count == 0:
                    break

                for card in cards.all():
                    item = extract_card(card, page.url)
                    if not item:
                        continue
                    nombre_norm = sin_tildes((item["nombre"] or "").lower())
                    if query_palabras and not nombre_norm.startswith(query_palabras[0]):
                        continue
                    key = item["url_producto"] or f'{item["nombre"]}|{item["precio_actual"]}'
                    if key in seen:
                        continue
                    seen.add(key)
                    products.append(item)

                # Si la página tiene menos de 40, normalmente es la última.
                if count < 40:
                    break
        finally:
            browser.close()

    products.sort(key=lambda p: (p["precio_unitario"] is None, p["precio_unitario"] or 0, p["nombre"]))
    return {
        "fuente": "santaisabel.cl",
        "url_consultada": base_url,
        "termino_busqueda": query,
        "fecha_consulta": extracted_at,
        "cantidad_productos": len(products),
        "productos": products,
    }


def save_outputs(result: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", result["termino_busqueda"].strip()).strip("_") or "busqueda"
    json_path = output_dir / f"santaisabel_{slug}.json"
    csv_path = output_dir / f"santaisabel_{slug}.csv"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    fields = [
        "nombre", "marca", "precio_actual", "precio_anterior", "precio_unitario",
        "texto_precio_unitario", "en_oferta", "calificacion", "url_producto",
        "url_imagen", "id_producto",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result["productos"])
    return json_path, csv_path


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

    # 1. Buscar el id del supermercado por nombre (ya existe: se creo al
    #    armar la tabla). Si no existe, se crea al vuelo.
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
            json={"nombre": NOMBRE_SUPERMERCADO, "sitio_web": "https://www.santaisabel.cl"},
            timeout=30,
        )
        crear.raise_for_status()
        supermercado_id = crear.json()[0]["id"]

    # 2. Armar las filas a insertar.
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

    # 3. Insertar en lotes (para no mandar un solo request gigante).
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Extrae productos y precios públicos de Santa Isabel")
    parser.add_argument("query", help="Término de búsqueda, por ejemplo: arroz")
    parser.add_argument("--max-pages", type=int, default=10, help="Máximo de páginas a recorrer")
    parser.add_argument("--pause", type=float, default=1.2, help="Pausa entre cargas, en segundos")
    parser.add_argument("--output-dir", default=".", help="Carpeta de salida")
    parser.add_argument("--visible", action="store_true", help="Muestra el navegador mientras trabaja")
    parser.add_argument(
        "--supabase", action="store_true",
        help="Ademas de guardar JSON/CSV local, sube los resultados a Supabase",
    )
    args = parser.parse_args()

    if args.max_pages < 1 or args.pause < 0:
        parser.error("--max-pages debe ser >= 1 y --pause debe ser >= 0")
    try:
        result = scrape(args.query, args.max_pages, args.pause, headless=not args.visible)
        json_path, csv_path = save_outputs(result, Path(args.output_dir))
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
