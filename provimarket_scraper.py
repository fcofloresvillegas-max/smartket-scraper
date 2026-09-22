#!/usr/bin/env python3
"""Descarga y extrae catálogos públicos de Provimarket.

Los catálogos de Provimarket están publicados como flipbooks de Heyzine,
pero cada flipbook expone un PDF público. Este script descarga esos PDF y
extrae candidatos de producto/precio usando la posicion (x, y) real de cada
palabra en la pagina (via pdfplumber) - no solo el orden del texto, porque
estos catalogos suelen venir en 2 columnas con productos a la misma altura
en ambos lados.

Uso:
    pip install requests pdfplumber
    python provimarket_scraper.py --termino arroz
    python provimarket_scraper.py --termino arroz --supabase
    python provimarket_scraper.py --output-dir datos --catalogo https://heyzine.com/flip-book/bee38e7cd8.html

Nota: los nombres y precios son CANDIDATOS heuristicos (adivinados por
cercania espacial en el PDF), no datos tan confiables como los scrapers de
sitios web estructurados. Revisa el JSON/CSV antes de subir a Supabase.
Las revistas pueden contener promociones 2x, 3x o precio por cuarto/kilo -
esos formatos ya se interpretan automaticamente (ver interpretar_precio).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import pdfplumber
import sys
import unicodedata
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

DEFAULT_CATALOGS = [
    "https://heyzine.com/flip-book/7aafd6237d.html",
    "https://heyzine.com/flip-book/02caafb045.html",
    "https://heyzine.com/flip-book/bee38e7cd8.html",
]
PROMOTIONS_URL = "https://provimarket.com/promociones/"
NOMBRE_SUPERMERCADO = "Provimarket"
UA = "ProvimarketCatalogReader/1.0"
PRICE_RE = re.compile(r"(?:\$\s*|CLP\s*)([0-9][0-9.]*)", re.I)
STANDALONE_PRICE_RE = re.compile(r"^(?:[0-9]{1,2}\.[0-9]{3}|[0-9]{3,5})(?:\s+[0-9])?$")
BULK_RE = re.compile(r"(\d+)\s*x\s*(?:\$|CLP)\s*([0-9][0-9.]*)", re.I)
POR_KG_RE = re.compile(r"(?:\$|CLP)\s*([0-9][0-9.]*)\s*(?:el\s*)?(?:por\s*)?kg\b|(?:\$|CLP)\s*([0-9][0-9.]*)\s*por\s*kilo", re.I)
EL_CUARTO_RE = re.compile(r"(?:\$|CLP)\s*([0-9][0-9.]*)\s*el\s*cuarto", re.I)
CADA_UNO_RE = re.compile(r"(?:\$|CLP)\s*([0-9][0-9.]*)\s*c\s*/\s*u\b", re.I)
VIGENCIA_RE = re.compile(
    r"v[aá]lid[oa]s?\s+(?:del|desde)?\s*([0-9]{1,2}\s+de\s+\w+|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)"
    r"\s*(?:al|hasta)\s*([0-9]{1,2}\s+de\s+\w+|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)",
    re.I,
)


def sin_tildes(texto: str) -> str:
    normalizado = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in normalizado if not unicodedata.combining(c))


def check_robots_allowed(url: str, user_agent: str = "*") -> bool:
    """Revisa robots.txt del host de la URL dada (heyzine.com/cdnm.heyzine.com)."""
    rp = urllib.robotparser.RobotFileParser()
    robots_url = urljoin(url, "/robots.txt")
    try:
        resp = requests.get(robots_url, timeout=20, headers={"User-Agent": UA})
        if resp.status_code == 404:
            return True
        resp.raise_for_status()
        rp.parse(resp.text.splitlines())
    except Exception as exc:
        print(f"[aviso] No se pudo leer robots.txt de {robots_url} ({exc}); se aborta por precaucion.")
        return False
    return rp.can_fetch(user_agent, url)


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def money(text: str) -> int | None:
    match = PRICE_RE.search(text or "")
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    value = int(digits) if digits else None
    return None if value is not None and value < 10 else value


def standalone_money(text: str) -> int | None:
    """Lee precios que pdftotext separa del símbolo $ por columnas."""
    value = clean(text).replace("$", "")
    if not STANDALONE_PRICE_RE.fullmatch(value):
        return None
    first = re.match(r"(?:[0-9]{1,2}\.[0-9]{3}|[0-9]{3,5})", value)
    if not first:
        return None
    digits = re.sub(r"\D", "", first.group(0))
    return int(digits) if digits else None


def interpretar_precio(line: str) -> dict[str, object] | None:
    """Interpreta formatos de precio de revista: '2 x $990' (2 unidades por
    $990 en total, no $990 c/u), '$1.560 el cuarto' (precio por 1/4 kilo),
    '$6.240 por kg' (ya es precio por kilo), '$850 c/u' (precio por unidad).
    Sin este manejo especial, un '2 x $990' se leeria como si el producto
    completo costara $990, subestimando el precio real a la mitad.
    """
    bulk = BULK_RE.search(line)
    if bulk:
        cantidad = int(bulk.group(1))
        total = money("$" + bulk.group(2))
        if cantidad > 0 and total is not None:
            return {
                "precio_actual": round(total / cantidad),
                "precio_unitario": None,
                "unidad": None,
                "texto_precio_unitario": None,
                "formato_detectado": f"{cantidad}x${total} (total, no c/u)",
            }

    cuarto = EL_CUARTO_RE.search(line)
    if cuarto:
        precio_cuarto = money("$" + cuarto.group(1))
        if precio_cuarto is not None:
            return {
                "precio_actual": precio_cuarto,
                "precio_unitario": precio_cuarto * 4,
                "unidad": "kg",
                "texto_precio_unitario": f"${precio_cuarto * 4} x kg (calculado desde 1/4 kilo)",
                "formato_detectado": "el cuarto",
            }

    kg = POR_KG_RE.search(line)
    if kg:
        precio_kg = money("$" + (kg.group(1) or kg.group(2)))
        if precio_kg is not None:
            return {
                "precio_actual": precio_kg,
                "precio_unitario": precio_kg,
                "unidad": "kg",
                "texto_precio_unitario": f"${precio_kg} x kg",
                "formato_detectado": "por kg",
            }

    cada_uno = CADA_UNO_RE.search(line)
    if cada_uno:
        precio_cu = money("$" + cada_uno.group(1))
        if precio_cu is not None:
            return {
                "precio_actual": precio_cu,
                "precio_unitario": None,
                "unidad": None,
                "texto_precio_unitario": None,
                "formato_detectado": "c/u",
            }

    precio_simple = money(line)
    if precio_simple is None:
        precio_simple = standalone_money(line)
    if precio_simple is not None:
        return {
            "precio_actual": precio_simple,
            "precio_unitario": None,
            "unidad": None,
            "texto_precio_unitario": None,
            "formato_detectado": "simple",
        }
    return None


def extraer_vigencia(pages_text: str) -> str | None:
    """Busca una linea tipo 'Válido del 25 de agosto al 5 de septiembre' en
    el texto del catalogo, para poder avisar si una promocion ya vencio en
    vez de mostrarla como si fuera el precio vigente hoy.
    """
    match = VIGENCIA_RE.search(pages_text)
    if match:
        return clean(f"Válido del {match.group(1)} al {match.group(2)}")
    return None


def catalog_pdf_url(flipbook_url: str) -> str:
    # La configuración del flipbook es pública dentro del HTML y contiene
    # el PDF original. Se toma la URL uploaded/…pdf publicada por Heyzine.
    html = requests.get(flipbook_url, headers={"User-Agent": UA}, timeout=30).text
    matches = re.findall(r"https://cdnm\.heyzine\.com/files/uploaded/(?:v3/)?[^\"' ]+?\.pdf", html)
    if not matches:
        raise RuntimeError(f"No se encontró PDF público para {flipbook_url}")
    return matches[0].replace("\\/", "/")


def extraer_candidatos_por_coordenadas(pdf_path: Path, flipbook_url: str, vigencia: str | None) -> list[dict[str, object]]:
    """Extrae candidatos usando la posicion (x, y) real de cada palabra en
    la pagina, en vez de leer el texto linea por linea de izquierda a
    derecha. Los catalogos de Provimarket suelen venir en 2 columnas con
    productos a la misma altura en ambos lados - leyendo solo por orden de
    lineas, el texto de la columna izquierda y derecha se mezcla y el
    nombre queda mal asociado al precio. Agrupando por cercania real en el
    espacio (misma "columna", no solo la misma altura) se evita eso.
    """
    records: list[dict[str, object]] = []
    ruido_re = re.compile(r"im[aá]genes|referenciales|vigencia|v[aá]lido|sujetos?\s*a\s*cambio", re.I)

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_no, page in enumerate(pdf.pages, 1):
            palabras = page.extract_words(x_tolerance=2, y_tolerance=2)
            if not palabras:
                continue

            # Agrupa palabras en "lineas" por altura (top) cercana.
            palabras_ordenadas = sorted(palabras, key=lambda w: (round(w["top"] / 3), w["x0"]))
            lineas_palabras: list[list[dict]] = []
            top_actual = None
            linea_actual: list[dict] = []
            for w in palabras_ordenadas:
                t = round(w["top"] / 3)
                if top_actual is None or t == top_actual:
                    linea_actual.append(w)
                    top_actual = t
                else:
                    lineas_palabras.append(linea_actual)
                    linea_actual = [w]
                    top_actual = t
            if linea_actual:
                lineas_palabras.append(linea_actual)

            lineas = []
            for grupo in lineas_palabras:
                grupo_ordenado = sorted(grupo, key=lambda w: w["x0"])
                lineas.append({
                    "texto": clean(" ".join(w["text"] for w in grupo_ordenado)),
                    "top": sum(w["top"] for w in grupo) / len(grupo),
                    "x0": min(w["x0"] for w in grupo),
                    "x1": max(w["x1"] for w in grupo),
                })
            lineas.sort(key=lambda l: l["top"])

            for idx, linea in enumerate(lineas):
                if ruido_re.search(linea["texto"]):
                    continue
                interpretado = interpretar_precio(linea["texto"])
                if interpretado is None:
                    continue

                # Busca el nombre en lineas anteriores, cercanas en altura
                # (misma "celda" del catalogo) Y que se solapen horizontalmente
                # con el precio (misma columna) - asi no cruza al otro lado
                # de la pagina.
                candidatos_nombre = []
                for prev in reversed(lineas[:idx]):
                    if linea["top"] - prev["top"] > 130:
                        break
                    solapa_horizontal = not (prev["x1"] < linea["x0"] - 150 or prev["x0"] > linea["x1"] + 150)
                    if not solapa_horizontal:
                        continue
                    if interpretar_precio(prev["texto"]) is not None:
                        continue
                    if ruido_re.search(prev["texto"]):
                        continue
                    if not re.search(r"[A-Za-zÁÉÍÓÚáéíóúÑñ]", prev["texto"]):
                        continue
                    candidatos_nombre.append(prev["texto"])
                    if len(candidatos_nombre) >= 2:
                        break

                if not candidatos_nombre:
                    continue
                nombre = " ".join(reversed(candidatos_nombre))

                records.append({
                    "nombre_candidato": nombre,
                    "precio": interpretado["precio_actual"],
                    "precio_unitario": interpretado["precio_unitario"],
                    "texto_precio_unitario": interpretado["texto_precio_unitario"],
                    "formato_detectado": interpretado["formato_detectado"],
                    "texto_precio": linea["texto"],
                    "pagina": page_no,
                    "catalogo": flipbook_url,
                    "vigencia": vigencia,
                    "fuente": flipbook_url,
                })
    return records


def extraer_texto_completo(pdf_path: Path) -> str:
    """Todo el texto del PDF, solo para buscar la vigencia (no se usa para
    asociar nombre-precio, eso lo hace extraer_candidatos_por_coordenadas)."""
    with pdfplumber.open(str(pdf_path)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def procesar_catalogos(catalogs: list[str], output: Path) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    output.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "es-CL,es;q=0.9"})
    all_rows: list[dict[str, object]] = []
    catalog_info: list[dict[str, object]] = []
    for index, flipbook in enumerate(catalogs, 1):
        try:
            if not check_robots_allowed(flipbook):
                raise RuntimeError(f"robots.txt de {urlparse(flipbook).netloc} no permite esta ruta.")
            pdf_url = catalog_pdf_url(flipbook)
            if not check_robots_allowed(pdf_url):
                raise RuntimeError(f"robots.txt de {urlparse(pdf_url).netloc} no permite descargar el PDF.")
            pdf_name = hashlib.sha1(pdf_url.encode()).hexdigest()[:12] + ".pdf"
            pdf_path = output / pdf_name
            response = session.get(pdf_url, timeout=90)
            response.raise_for_status()
            pdf_path.write_bytes(response.content)
            texto_completo = extraer_texto_completo(pdf_path)
            vigencia = extraer_vigencia(texto_completo)
            rows = extraer_candidatos_por_coordenadas(pdf_path, flipbook, vigencia)
            with pdfplumber.open(str(pdf_path)) as pdf_abierto:
                num_paginas = len(pdf_abierto.pages)
            all_rows.extend(rows)
            catalog_info.append({
                "flipbook": flipbook, "pdf": pdf_url, "paginas": num_paginas,
                "candidatos": len(rows), "vigencia": vigencia,
            })
            aviso_vigencia = f" (vigencia: {vigencia})" if vigencia else " (no se detectó vigencia - puede que este catálogo no la publique)"
            print(f"[{index}/{len(catalogs)}] {num_paginas} páginas, {len(rows)} candidatos{aviso_vigencia}: {flipbook}")
        except Exception as exc:
            print(f"[aviso] No se pudo procesar {flipbook}: {exc}", file=sys.stderr)
    return all_rows, catalog_info


def filtrar_por_termino(rows: list[dict[str, object]], termino: str) -> list[dict[str, object]]:
    query_palabras = [w for w in sin_tildes(termino.strip().lower()).split() if w]
    if not query_palabras:
        return rows
    filtrados = []
    for r in rows:
        nombre_norm = sin_tildes(str(r.get("nombre_candidato", "")).lower())
        if nombre_norm.startswith(query_palabras[0]):
            filtrados.append(r)
    return filtrados


_CANDIDATOS_CACHE: list[dict[str, object]] | None = None


def scrape(termino: str, output_dir: str = ".") -> dict[str, object]:
    """Interfaz estandar (igual a los demas scrapers): descarga y procesa
    las revistas UNA sola vez por corrida (con cache en memoria), y despues
    solo filtra por termino - no vuelve a bajar los PDF por cada producto.
    """
    global _CANDIDATOS_CACHE
    if _CANDIDATOS_CACHE is None:
        _CANDIDATOS_CACHE, _ = procesar_catalogos(DEFAULT_CATALOGS, Path(output_dir))

    filtrados = filtrar_por_termino(_CANDIDATOS_CACHE, termino)
    return {
        "fuente": PROMOTIONS_URL,
        "url_consultada": PROMOTIONS_URL,
        "termino_busqueda": termino,
        "fecha_consulta": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "cantidad_productos": len(filtrados),
        "productos": filtrados,
    }


def subir_a_supabase(result) -> int:
    """Inserta los candidatos (ya filtrados por termino) en precios_productos.

    IMPORTANTE: a diferencia de los otros scrapers, estos son CANDIDATOS
    heuristicos (nombre y precio adivinados por posicion en un PDF de
    revista), no datos estructurados confiables. Revisa el JSON local antes
    de usar esto - un nombre mal asociado a un precio ensucia el comparador.
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
            json={"nombre": NOMBRE_SUPERMERCADO, "sitio_web": "https://provimarket.com"},
            timeout=30,
        )
        crear.raise_for_status()
        supermercado_id = crear.json()[0]["id"]

    fecha_consulta = result.get("fecha_consulta") or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    termino = result["termino_busqueda"]
    rows = result["productos"]
    filas_a_insertar = []
    for r in rows:
        filas_a_insertar.append({
            "supermercado_id": supermercado_id,
            "termino_busqueda": termino,
            "id_producto_tienda": None,
            "nombre": r.get("nombre_candidato"),
            "marca": None,
            "precio_actual": r.get("precio"),
            "precio_anterior": None,
            "precio_unitario": r.get("precio_unitario"),
            "unidad": r.get("texto_precio_unitario") or (f"vigencia: {r['vigencia']}" if r.get("vigencia") else None),
            "en_oferta": True,
            "calificacion": None,
            "url_producto": r.get("fuente"),
            "url_imagen": None,
            "fecha_consulta": fecha_consulta,
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Extrae catálogos públicos de Provimarket")
    parser.add_argument("--output-dir", default="datos_provimarket")
    parser.add_argument("--catalogo", action="append", dest="catalogs", help="URL de Heyzine; se puede repetir")
    parser.add_argument("--termino", help="Filtra los candidatos por un termino, ej: arroz")
    parser.add_argument(
        "--supabase", action="store_true",
        help="Con --termino: sube los candidatos filtrados a Supabase (revisa el JSON primero, son heuristicos)",
    )
    args = parser.parse_args()
    output = Path(args.output_dir)
    catalogs = args.catalogs or DEFAULT_CATALOGS
    all_rows, catalog_info = procesar_catalogos(catalogs, output)

    if args.termino:
        filtrados = filtrar_por_termino(all_rows, args.termino)
        print(f"\nCandidatos para '{args.termino}': {len(filtrados)}")
        for r in filtrados[:15]:
            vig = f" [{r['vigencia']}]" if r.get("vigencia") else " [vigencia no detectada]"
            print(f"  ${r['precio']} - {r['nombre_candidato']} ({r['formato_detectado']}){vig}")
        if args.supabase:
            try:
                resultado_para_subir = {
                    "termino_busqueda": args.termino,
                    "productos": filtrados,
                    "fecha_consulta": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                }
                insertados = subir_a_supabase(resultado_para_subir)
                print(f"Supabase: {insertados} filas insertadas en precios_productos.")
            except Exception as exc:
                print(f"Error subiendo a Supabase: {exc}", file=sys.stderr)
                return 1
        return 0

    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    result = {
        "fuente_promociones": PROMOTIONS_URL,
        "fecha_consulta": stamp,
        "catalogos": catalog_info,
        "productos_candidatos": all_rows,
        "nota": (
            "Estos son CANDIDATOS heuristicos (nombre y precio adivinados por "
            "posicion en un PDF de revista), no datos estructurados confiables "
            "como los otros scrapers. Revisa manualmente antes de usar --supabase. "
            "Los formatos '2x$', 'el cuarto' y 'por kg' ya se interpretan solos "
            "(ver 'formato_detectado' y 'precio_unitario' en cada candidato), "
            "pero la vigencia de cada catalogo puede no detectarse siempre - "
            "revisa el campo 'vigencia' antes de dar por buena una promocion."
        ),
    }
    json_path = output / "provimarket_catalogos.json"
    csv_path = output / "provimarket_catalogos.csv"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = [
        "nombre_candidato", "precio", "precio_unitario", "texto_precio_unitario",
        "formato_detectado", "texto_precio", "pagina", "catalogo", "vigencia", "fuente",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"JSON: {json_path.resolve()}")
    print(f"CSV:  {csv_path.resolve()}")
    print(f"Candidatos extraídos: {len(all_rows)}")
    return 0 if catalog_info else 1


if __name__ == "__main__":
    raise SystemExit(main())
