#!/usr/bin/env python3
"""Extrae productos y precios de búsquedas públicas de A Cuenta.cl.

Uso:
    python acuenta_scraper.py arroz
    python acuenta_scraper.py arroz --output-dir datos

Requisitos:
    pip install playwright
    playwright install chromium
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
from requests.packages.urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE_URL = "https://www.acuenta.cl/search?name={query}"
NOMBRE_SUPERMERCADO = "A Cuenta"


def sin_tildes(texto: str) -> str:
    """Quita tildes para comparar texto sin que 'cafe' != 'café' cause problemas."""
    normalizado = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in normalizado if not unicodedata.combining(c))


def check_robots_allowed(url: str, user_agent: str = "*") -> bool:
    """Revisa robots.txt de acuenta.cl antes de scrapear la URL dada.

    acuenta.cl tiene un certificado con la cadena incompleta (falta el
    intermedio) - los navegadores lo toleran encadenando automaticamente,
    pero clientes como requests/urllib no. Como el scraper ya visita la
    pagina real con ignore_https_errors=True (mismo nivel de confianza),
    aplicamos ese mismo criterio acá para no bloquearnos a nosotros mismos
    por un problema del propio sitio, no de seguridad de quien lo visita.
    """
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


def clean_text(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def money_to_int(value: str | None) -> int | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    return int(digits) if digits else None


def dismiss_cookies(page) -> None:
    for label in ("Aceptar todas las cookies", "Aceptar", "Continuar", "Entendido"):
        try:
            button = page.get_by_role("button", name=re.compile(label, re.I)).first
            if button.is_visible(timeout=700):
                button.click(timeout=1500)
                return
        except Exception:
            pass


def extract_card(card, base_url: str) -> dict[str, Any] | None:
    name_node = card.locator('[data-testid="card-name"]').first
    if not name_node.count():
        return None
    name = clean_text(name_node.inner_text())
    if not name:
        return None

    price_node = card.locator('[data-testid="card-base-price"]').first
    current = money_to_int(price_node.inner_text() if price_node.count() else None)

    unit_node = card.locator(".prod__pum").first
    unit_text = clean_text(unit_node.inner_text() if unit_node.count() else None)
    unit_match = re.search(r"\$\s*([\d.]+)\s*por\s*([^\s\)]+)", unit_text or "", re.I)
    unit_price = money_to_int(unit_match.group(1)) if unit_match else None
    unit_label = clean_text(unit_match.group(2)) if unit_match else None

    link_node = card.locator('a.containerCard').first
    href = link_node.get_attribute("href") if link_node.count() else None
    image_node = card.locator('[data-testid="product-image-main"] img').first
    image = image_node.get_attribute("src") if image_node.count() else None
    product_id = card.get_attribute("data-product-sku")
    full_text = card.inner_text()

    old = None
    old_match = re.search(r"\(\s*\$\s*([\d.]+)\s*\)", full_text)
    if old_match:
        old = money_to_int(old_match.group(1))
    if old is not None and current is not None and old <= current:
        old = None

    return {
        "nombre": name,
        "marca": None,
        "precio_actual": current,
        "precio_anterior": old,
        "precio_unitario": unit_price,
        "unidad_precio_unitario": unit_label,
        "en_oferta": old is not None or bool(re.search(r"x\s*\$|oferta|descuento|ahorra", full_text, re.I)),
        "calificacion": None,
        "url_producto": urljoin(base_url, href) if href else None,
        "url_imagen": image,
        "id_producto": product_id,
    }


def load_all_cards(page, pause: float, max_pages: int) -> None:
    """Recorre la paginación simple de A Cuenta (por ejemplo, 1/2)."""
    for _ in range(max_pages - 1):
        next_button = page.locator(
            'li.ant-pagination-next:not(.ant-pagination-disabled) button[aria-label="Next Page"], '
            'li.ant-pagination-next:not(.ant-pagination-disabled) button.ant-pagination-item-link'
        ).first
        if not next_button.count() or not next_button.is_visible():
            break
        before = page.locator('[data-testid="card-name"]').first.inner_text()
        next_button.click()
        page.wait_for_timeout(int(max(pause, 0.8) * 1000))
        page.wait_for_function(
            "([selector, previous]) => { const el = document.querySelector(selector); return el && el.innerText !== previous; }",
            arg=['[data-testid="card-name"]', before],
            timeout=30000,
        )


def scrape(query: str, pause: float = 1.0, max_pages: int = 10, headless: bool = True) -> dict[str, Any]:
    encoded = quote_plus(query.strip())
    url = BASE_URL.format(query=encoded)

    if not check_robots_allowed(url):
        raise RuntimeError("robots.txt de acuenta.cl no permite esta ruta.")

    extracted_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    query_palabras = [w for w in sin_tildes(query.strip().lower()).split() if w]
    products: list[dict[str, Any]] = []
    seen: set[str] = set()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        page = browser.new_page(
            locale="es-CL",
            viewport={"width": 1440, "height": 1000},
            ignore_https_errors=True,
        )
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector('[data-testid="card-name"]', timeout=60000)
            dismiss_cookies(page)
            page.wait_for_timeout(int(max(pause, 1.0) * 1000))
            total_pages = 1
            try:
                page.wait_for_selector(".ant-pagination", timeout=8000)
                pagination_text = page.locator(".ant-pagination").first.inner_text()
                numeros = re.findall(r"\d+", pagination_text)
                if numeros:
                    total_pages = int(numeros[-1])
            except PlaywrightTimeoutError:
                pass  # sin paginacion visible: se asume que solo hay una pagina
            total_pages = min(total_pages, max_pages)
            selector = '[data-testid$="productCard"]:has([data-testid="card-name"])'
            for page_number in range(total_pages):
                for card in page.locator(selector).all():
                    item = extract_card(card, url)
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
                if page_number < total_pages - 1:
                    load_all_cards(page, pause, 2)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError("A Cuenta no cargó los productos. Verifica que la página esté accesible en el navegador.") from exc
        finally:
            browser.close()

    products.sort(key=lambda p: (p["precio_unitario"] is None, p["precio_unitario"] or 0, p["nombre"]))
    return {
        "fuente": "acuenta.cl",
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
            json={"nombre": NOMBRE_SUPERMERCADO, "sitio_web": "https://www.acuenta.cl"},
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
            "unidad": p.get("unidad_precio_unitario"),
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
    json_path = output_dir / f"acuenta_{slug}.json"
    csv_path = output_dir / f"acuenta_{slug}.csv"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = [
        "nombre", "marca", "precio_actual", "precio_anterior", "precio_unitario",
        "unidad_precio_unitario", "en_oferta", "calificacion", "url_producto",
        "url_imagen", "id_producto",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result["productos"])
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Extrae productos y precios públicos de A Cuenta.cl")
    parser.add_argument("query", help="Término de búsqueda, por ejemplo: arroz")
    parser.add_argument("--pause", type=float, default=1.0, help="Pausa entre scrolls, en segundos")
    parser.add_argument("--max-pages", type=int, default=10, help="Máximo de páginas a recorrer")
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
        result = scrape(args.query, args.pause, args.max_pages, headless=not args.visible)
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
