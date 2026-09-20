#!/usr/bin/env python3
"""Extractor local y visible para la primera página de resultados de Unimarc.

Flujo por cada búsqueda:
  1. Abre un navegador visible en una sesión nueva.
  2. Espera productos y permite resolver manualmente CAPTCHA/Akamai.
  3. Intenta seleccionar orden descendente por precio.
  4. Lee solo los productos de la primera página.
  5. Ordena localmente de mayor a menor como respaldo.
  6. Guarda JSON/CSV, cierra la sesión y continúa con la siguiente búsqueda.

Uso en Windows:
    pip install playwright
    python -m playwright install chromium
    python unimarc_scraper.py arroz leche

No evade CAPTCHA ni controles de acceso: si aparece una verificación, debes
completarla manualmente en la ventana visible.
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
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE_URL = "https://www.unimarc.cl/search?q={query}&suggestions=true"
PRICE_RE = re.compile(r"\$\s*([\d.]+)")
WEIGHT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|kilos?|g|gr|gramos)\b", re.IGNORECASE)
VOLUME_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(lt|l|litros?|ml|cc)\b", re.IGNORECASE)
NOMBRE_SUPERMERCADO = "Unimarc"


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


def check_robots_allowed(page, url: str, user_agent: str = "*") -> bool:
    """Revisa robots.txt de unimarc.cl antes de abrir la pagina de busqueda.

    unimarc.cl (Akamai) bloquea con 403 cualquier request hecho con la
    libreria requests, incluso con un user-agent de navegador normal -
    parece detectar algo mas profundo (huella TLS/JS) que un cliente HTTP
    simple no puede replicar. Por eso se revisa con el MISMO navegador
    (Playwright) que ya usa el script para todo lo demas, en vez de un
    request aparte.
    """
    rp = urllib.robotparser.RobotFileParser()
    robots_url = urljoin(url, "/robots.txt")
    try:
        response = page.goto(robots_url, wait_until="domcontentloaded", timeout=30000)
        if response is not None and response.status == 404:
            # No existe robots.txt: por convencion estandar, eso significa
            # que no hay restricciones publicadas (distinto a un bloqueo
            # real via Disallow).
            return True
        if response is None or not response.ok:
            # Akamai devuelve Access Denied específicamente para robots.txt,
            # aunque la página pública de resultados sí pueda abrirse. No se
            # interpreta esa respuesta como un Disallow; la URL se abrirá y
            # cualquier CAPTCHA seguirá requiriendo resolución manual.
            print(f"[aviso] Unimarc no permitió leer robots.txt (estado {response.status if response else '?'}); se continúa solo con la página pública.")
            return True
        texto = page.locator("body").inner_text(timeout=5000)
        rp.parse(texto.splitlines())
    except Exception as exc:
        print(f"[aviso] No se pudo leer robots.txt ({exc}); se aborta por precaucion.")
        return False
    permitido = rp.can_fetch(user_agent, url)
    if not permitido:
        print("[aviso] robots.txt indica que esta URL no está permitida.")
    return permitido


def clean(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def money(value: str | None) -> int | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    return int(digits) if digits else None


def wait_for_human_check(page, timeout: int) -> None:
    """Pausa ante CAPTCHA; nunca intenta resolverlo automáticamente."""
    text = page.locator("body").inner_text(timeout=10000)
    challenge = re.search(r"captcha|verifica|no eres un robot|security|access denied|akamai", text, re.I)
    if not challenge:
        return
    print("\nUnimarc mostró una verificación de seguridad.")
    print("Completa manualmente el CAPTCHA en la ventana del navegador.")
    print("Después presiona ENTER aquí para que el script continúe.")
    input()
    page.wait_for_timeout(1500)
    try:
        page.wait_for_selector("body", timeout=timeout * 1000)
    except PlaywrightTimeoutError:
        raise RuntimeError("La página no terminó de cargar después de la verificación manual.")


def try_sort_ascending(page, wait_ms: int) -> bool:
    """Intenta usar el control visible de Unimarc, de menor a mayor precio."""
    patterns = [
        re.compile(r"ordenar", re.I),
        re.compile(r"menor precio|precio.*bajo|menor a mayor|más barato", re.I),
    ]
    for pattern in patterns:
        for role in ("button", "combobox", "listbox"):
            try:
                loc = page.get_by_role(role, name=pattern).first
                if loc.count() and loc.is_visible(timeout=800):
                    loc.click(timeout=2000)
                    page.wait_for_timeout(wait_ms)
                    break
            except Exception:
                pass
    option_patterns = [
        re.compile(r"precio.*menor|menor.*precio|más barato|menor a mayor", re.I),
    ]
    for pattern in option_patterns:
        try:
            loc = page.get_by_text(pattern).last
            if loc.count() and loc.is_visible(timeout=800):
                loc.click(timeout=2000)
                page.wait_for_timeout(wait_ms)
                return True
        except Exception:
            pass
    return False


def extract_products(page, termino: str) -> list[dict[str, object]]:
    """Extrae tarjetas de producto usando el id estable que usa Unimarc
    para cada una: id="shelf__vertical--<slug-del-producto>". Unimarc no
    usa <button> reales para "Agregar" (es un <div> con un icono SVG y
    aria-label, sin texto visible), por eso las heuristicas basadas en
    botones con texto no encontraban nada - esto es mas confiable.
    """
    raw = page.evaluate(
        """() => {
          const cards = [...document.querySelectorAll('[id^="shelf__vertical--"]')];
          return cards.map(c => ({id: c.id, text: (c.innerText || '').trim()}));
        }"""
    )

    query_palabras = [w for w in sin_tildes(termino.strip().lower()).split() if w]
    price_re = re.compile(r"^\$\s*[\d.]+$")
    unit_price_re = re.compile(r"^\(\$\s*([\d.]+)\s*x\s*(\w+)\)$", re.IGNORECASE)
    percent_re = re.compile(r"^\d+%$")
    boilerplate = {"club unimarc", "agregar"}

    products = []
    seen = set()
    for item in raw:
        text = str(item.get("text", ""))
        lines = [clean(l) for l in text.split("\n") if clean(l)]
        lines = [l for l in lines if l.casefold() not in boilerplate and not percent_re.match(l)]
        if not lines:
            continue

        price_lines = [l for l in lines if price_re.match(l)]
        unit_price_lines = [l for l in lines if unit_price_re.match(l)]
        text_lines = [l for l in lines if l not in price_lines and l not in unit_price_lines]
        if not text_lines:
            continue

        name = max(text_lines, key=len)
        nombre_norm = sin_tildes(name.lower())
        if query_palabras and not nombre_norm.startswith(query_palabras[0]):
            continue

        marca = None
        idx = text_lines.index(name)
        if idx > 0:
            candidato = text_lines[idx - 1]
            if candidato.casefold() != name.casefold() and len(candidato) < len(name):
                marca = candidato

        current = money(price_lines[0]) if price_lines else None
        old = money(price_lines[1]) if len(price_lines) > 1 else None
        if old is not None and current is not None and old <= current:
            old = None

        unit_price = None
        unit_label = None
        if unit_price_lines:
            m = unit_price_re.match(unit_price_lines[0])
            if m:
                unit_price = money(m.group(1))
                unit_label = f"${unit_price} x {m.group(2)}"

        key = item.get("id") or f"{name}|{current}"
        if key in seen:
            continue
        seen.add(key)

        products.append({
            "nombre": name,
            "marca": marca,
            "precio_actual": current,
            "precio_anterior": old,
            "precio_unitario": unit_price,
            "texto_precio_unitario": unit_label,
            "en_oferta": old is not None,
            "url_producto": None,
            "id_producto": item.get("id"),
            "fuente": "https://www.unimarc.cl",
        })
    products.sort(key=lambda p: (p["precio_actual"] is None, p["precio_actual"] or 0))
    return products


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
            json={"nombre": NOMBRE_SUPERMERCADO, "sitio_web": "https://www.unimarc.cl"},
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
            "url_imagen": None,
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


def save(products: list[dict[str, object]], query: str, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", query).strip("_").lower() or "busqueda"
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    result = {
        "fuente": BASE_URL.format(query=quote_plus(query)),
        "termino_busqueda": query,
        "fecha_consulta": now,
        "pagina_extraida": 1,
        "orden": "precio_actual_ascendente",
        "cantidad_productos": len(products),
        "productos": products,
    }
    json_path = output_dir / f"unimarc_{slug}.json"
    csv_path = output_dir / f"unimarc_{slug}.csv"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = ["nombre", "marca", "precio_actual", "precio_anterior", "precio_unitario", "texto_precio_unitario", "en_oferta", "url_producto", "id_producto", "fuente"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(products)
    return json_path, csv_path


def scrape(query: str, wait_seconds: int = 180, pause: float = 2.0) -> dict[str, object]:
    """Version que devuelve el resultado en memoria (para --supabase o para
    que run_nightly.py la use), en vez de solo guardar archivos.
    """
    url = BASE_URL.format(query=quote_plus(query))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context(locale="es-CL", viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        try:
            print(f"\nAbriendo Unimarc para: {query}")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            wait_for_human_check(page, wait_seconds)
            page.wait_for_function("() => /\\$\\s*[0-9]/.test(document.body.innerText)", timeout=60000)
            page.wait_for_timeout(int(pause * 1000))
            sorted_by_site = try_sort_ascending(page, int(pause * 1000))
            if sorted_by_site:
                print("Ordenamiento ascendente aplicado en la página.")
            else:
                print("No se detectó el control de orden; se ordenarán los productos extraídos localmente.")
            page.wait_for_timeout(int(pause * 1000))
            products = extract_products(page, query)
        finally:
            context.close()
            browser.close()
            print(f"Sesión cerrada para: {query}")

    return {
        "fuente": url,
        "url_consultada": url,
        "termino_busqueda": query,
        "fecha_consulta": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "cantidad_productos": len(products),
        "productos": products,
    }


def process_query(playwright, query: str, output_dir: Path, wait_seconds: int, pause: float) -> None:
    # Se crea un proceso de navegador nuevo para cada consulta, tal como se
    # necesita para no reutilizar cookies, CAPTCHA ni estado de la búsqueda.
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context(locale="es-CL", viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    try:
        url = BASE_URL.format(query=quote_plus(query))
        print(f"\nAbriendo Unimarc para: {query}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        wait_for_human_check(page, wait_seconds)
        page.wait_for_function("() => /\\$\\s*[0-9]/.test(document.body.innerText)", timeout=60000)
        page.wait_for_timeout(int(pause * 1000))
        sorted_by_site = try_sort_ascending(page, int(pause * 1000))
        if sorted_by_site:
            print("Ordenamiento ascendente aplicado en la página.")
        else:
            print("No se detectó el control de orden; se ordenarán los productos extraídos localmente.")
        page.wait_for_timeout(int(pause * 1000))
        products = extract_products(page, query)
        if not products:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(output_dir) / f"unimarc_{re.sub(r'[^a-zA-Z0-9_-]+', '_', query).strip('_').lower() or 'busqueda'}_diagnostico.png"), full_page=True)
            Path(output_dir, f"unimarc_{re.sub(r'[^a-zA-Z0-9_-]+', '_', query).strip('_').lower() or 'busqueda'}_diagnostico.html").write_text(page.content(), encoding="utf-8")
            raise RuntimeError("No se detectaron tarjetas. Revisa el CAPTCHA o la estructura de Unimarc.")
        jp, cp = save(products, query, output_dir)
        print(f"Productos de la primera hoja: {len(products)}")
        print(f"JSON: {jp.resolve()}")
        print(f"CSV:  {cp.resolve()}")
    finally:
        context.close()
        browser.close()
        print(f"Sesión cerrada para: {query}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Scraper local visible de primera página de Unimarc")
    parser.add_argument("queries", nargs="+", help="Búsquedas, por ejemplo: arroz leche aceite")
    parser.add_argument("--output-dir", default=".", help="Carpeta de salida (por defecto, la carpeta actual)")
    parser.add_argument("--wait-seconds", type=int, default=180, help="Tiempo para completar CAPTCHA")
    parser.add_argument("--pause", type=float, default=2.0, help="Pausa entre carga y extracción")
    parser.add_argument(
        "--supabase", action="store_true",
        help="Ademas de guardar JSON/CSV local, sube los resultados a Supabase",
    )
    args = parser.parse_args()
    if args.wait_seconds < 1 or args.pause < 0:
        parser.error("--wait-seconds debe ser >= 1 y --pause >= 0")
    with sync_playwright() as playwright:
        for query in args.queries:
            try:
                process_query(playwright, query, Path(args.output_dir), args.wait_seconds, args.pause)
            except Exception as exc:
                print(f"Error en '{query}': {exc}", file=sys.stderr)
                time.sleep(1)
                continue
            if args.supabase:
                try:
                    jp = Path(args.output_dir) / f"unimarc_{re.sub(r'[^a-zA-Z0-9_-]+', '_', query).strip('_').lower() or 'busqueda'}.json"
                    result = json.loads(jp.read_text(encoding="utf-8"))
                    insertados = subir_a_supabase(result)
                    print(f"Supabase: {insertados} filas insertadas en precios_productos.")
                except Exception as exc:
                    print(f"Error subiendo a Supabase '{query}': {exc}", file=sys.stderr)
            time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
