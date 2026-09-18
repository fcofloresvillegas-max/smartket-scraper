#!/usr/bin/env python3
"""Extrae productos y precios de búsquedas públicas de Tottus.cl.

Uso:
    python tottus_scraper.py arroz
    python tottus_scraper.py arroz --max-pages 10 --output-dir datos

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urljoin, urlsplit

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE_URL = (
    "https://www.tottus.cl/tottus-cl/buscar?Ntt={query}"
    "&sortBy=derived.currentPrice%2Casc"
    "&latLong=%257B%2522latitude%2522%253A%2522-33.4161207%2522%252C"
    "%2522longitude%2522%253A%2522-70.5419009%2522%257D"
)
NOMBRE_SUPERMERCADO = "Tottus"


def sin_tildes(texto: str) -> str:
    """Quita tildes para comparar texto sin que 'cafe' != 'café' cause problemas."""
    normalizado = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in normalizado if not unicodedata.combining(c))


def check_robots_allowed(url: str, user_agent: str = "*") -> bool:
    """Revisa robots.txt sin el falso bloqueo de grupos User-agent duplicados.

    El robots.txt actual de Tottus contiene dos bloques ``User-agent: *``.
    urllib.robotparser conserva el segundo bloque y termina tratando las rutas
    no mencionadas como bloqueadas. Aquí solo consideramos bloqueadas las
    rutas Disallow no vacías que realmente coinciden con la ruta consultada.
    """
    try:
        robots_url = urljoin(url, "/robots.txt")
        response = requests.get(robots_url, timeout=20)
        response.raise_for_status()
    except Exception as exc:
        print(f"[aviso] No se pudo leer robots.txt ({exc}); se aborta por precaucion.")
        return False

    path = urlsplit(url).path or "/"
    for raw_line in response.text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        directive, value = line.split(":", 1)
        if directive.strip().lower() != "disallow":
            continue
        blocked = value.strip()
        if blocked and path.startswith(blocked.rstrip("*")):
            return False
    return True


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


def wait_for_cloudflare(page, timeout_seconds: int = 180) -> None:
    """Espera una verificación humana de Cloudflare sin intentar evadirla.

    Si timeout_seconds es bajo (uso automatico sin nadie mirando), simplemente
    falla rapido en vez de quedarse esperando a que alguien resuelva el check.
    """
    challenge = page.get_by_text(re.compile(r"Verificaci[oó]n de seguridad|Verifique que es un ser humano", re.I)).first
    try:
        if not challenge.is_visible(timeout=1000):
            return
    except Exception:
        return

    print("\nTottus activó una verificación de Cloudflare.")
    print("Marca manualmente la casilla 'Verifique que es un ser humano' en la ventana del navegador.")
    print(f"El scraper esperará hasta {timeout_seconds} segundos y continuará cuando aparezcan los productos.\n")
    try:
        page.wait_for_selector('[data-testid="ssr-pod"]', state="visible", timeout=timeout_seconds * 1000)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("La verificación de Cloudflare no fue completada a tiempo.") from exc


def extract_card(card, base_url: str) -> dict[str, Any] | None:
    title_node = card.locator(".pod-subTitle").first
    name = clean_text(title_node.inner_text()) if title_node.count() else None
    if not name:
        return None

    brand_node = card.locator(".pod-title").first
    brand = clean_text(brand_node.inner_text()) if brand_node.count() else None
    unit_node = card.locator(".pod-subtitle-unit").first
    unit = clean_text(unit_node.inner_text()) if unit_node.count() else None

    price_node = card.locator('li[data-internet-price]').first
    current = money_to_int(price_node.get_attribute("data-internet-price")) if price_node.count() else None
    old_node = card.locator('li[data-normal-price]').first
    old = money_to_int(old_node.get_attribute("data-normal-price")) if old_node.count() else None
    if old is not None and current is not None and old <= current:
        old = None

    unit_text = None
    if price_node.count():
        unit_text = clean_text(price_node.inner_text())
    unit_match = re.search(r"\(\$\s*([\d.]+)\s*por\s*([^\)]+)\)", unit_text or "", re.I)
    unit_price = money_to_int(unit_match.group(1)) if unit_match else None
    unit_label = clean_text(unit_match.group(2)) if unit_match else None

    link = card.locator('a[href*="/tottus-cl/articulo/"]').first
    href = link.get_attribute("href") if link.count() else None
    image_node = card.locator("img").first
    image = image_node.get_attribute("src") if image_node.count() else None
    product_id = card.get_attribute("data-key") or card.get_attribute("id")
    available = "Envío" in (card.inner_text() or "") or "Retiro" in (card.inner_text() or "")

    return {
        "nombre": name,
        "marca": brand,
        "precio_actual": current,
        "precio_anterior": old,
        "precio_unitario": unit_price,
        "unidad_precio_unitario": unit_label,
        "formato": unit,
        "en_oferta": old is not None,
        "calificacion": None,
        "disponible": available,
        "url_producto": href,
        "url_imagen": image,
        "id_producto": product_id,
    }


def scrape(query: str, max_pages: int = 10, pause: float = 1.0, headless: bool = True, cloudflare_timeout: int = 180) -> dict[str, Any]:
    encoded = quote_plus(query.strip())
    url = BASE_URL.format(query=encoded)

    if not check_robots_allowed(url):
        raise RuntimeError("robots.txt de tottus.cl no permite esta ruta.")

    extracted_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    query_palabras = [w for w in sin_tildes(query.strip().lower()).split() if w]
    products: list[dict[str, Any]] = []
    seen: set[str] = set()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        page = browser.new_page(locale="es-CL", viewport={"width": 1440, "height": 1000})
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            wait_for_cloudflare(page, timeout_seconds=cloudflare_timeout)
            page.wait_for_selector('[data-testid="ssr-pod"]', timeout=60000)
            dismiss_cookies(page)
            page.wait_for_timeout(int(max(pause, 0.5) * 1000))

            page_buttons = page.locator('button[id^="testId-pagination-top-button"]')
            detected_pages = page_buttons.count()
            total_pages = min(max(detected_pages, 1), max_pages)

            for number in range(1, total_pages + 1):
                if number > 1:
                    button = page.locator(f"#testId-pagination-top-button{number}")
                    if not button.count():
                        break
                    button.click()
                    page.wait_for_timeout(int(max(pause, 0.8) * 1000))
                    wait_for_cloudflare(page, timeout_seconds=cloudflare_timeout)
                    page.wait_for_selector('[data-testid="ssr-pod"]', timeout=30000)

                for card in page.locator('[data-testid="ssr-pod"]').all():
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
        except PlaywrightTimeoutError as exc:
            raise RuntimeError("Tottus no cargó los productos. Verifica que Cloudflare o el navegador tengan acceso al sitio.") from exc
        finally:
            browser.close()

    products.sort(key=lambda p: (p["precio_unitario"] is None, p["precio_unitario"] or 0, p["nombre"]))
    return {
        "fuente": "tottus.cl",
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
            json={"nombre": NOMBRE_SUPERMERCADO, "sitio_web": "https://www.tottus.cl"},
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
            "unidad": p.get("unidad_precio_unitario") or p.get("formato"),
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
    json_path = output_dir / f"tottus_{slug}.json"
    csv_path = output_dir / f"tottus_{slug}.csv"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = [
        "nombre", "marca", "precio_actual", "precio_anterior", "precio_unitario",
        "unidad_precio_unitario", "formato", "en_oferta", "calificacion", "disponible",
        "url_producto", "url_imagen", "id_producto",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result["productos"])
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Extrae productos y precios públicos de Tottus.cl")
    parser.add_argument("query", help="Término de búsqueda, por ejemplo: arroz")
    parser.add_argument("--max-pages", type=int, default=1, help="Máximo de páginas a recorrer (por defecto, solo la primera)")
    parser.add_argument("--pause", type=float, default=1.0, help="Pausa entre páginas, en segundos")
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
