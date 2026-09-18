#!/usr/bin/env python3
"""
run_nightly.py

Corre los scrapers de Jumbo y Lider para cada producto de canasta_basica.py
y sube los resultados a Supabase. Pensado para ejecutarse una vez por noche
desde GitHub Actions (ver .github/workflows/nightly.yml), pero tambien se
puede correr a mano para probar.

Uso:
    python run_nightly.py
    python run_nightly.py --limite 5              (solo los primeros 5 terminos)
    python run_nightly.py --tiendas jumbo          (solo Jumbo)
    python run_nightly.py --tiendas santaisabel tottus --visible   (a mano, con navegador visible)

Requiere las mismas variables de entorno que los scrapers --supabase:
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY
"""

from __future__ import annotations

import argparse
import random
import sys
import time

from canasta_basica import CANASTA_BASICA
import jumbo_scraper
import lider_scraper
import santaisabel_scraper
import acuenta_scraper
import tottus_scraper

TIENDAS = {
    "jumbo": {
        "nombre": "Jumbo",
        "scrape": lambda termino, headless: jumbo_scraper.scrape(termino, max_pages=5, pause=1.2, headless=headless),
        "subir": jumbo_scraper.subir_a_supabase,
    },
    "lider": {
        "nombre": "Lider",
        "scrape": lambda termino, headless: lider_scraper.scrape(termino, pause=1.0),
        "subir": lider_scraper.subir_a_supabase,
    },
    "santaisabel": {
        "nombre": "Santa Isabel",
        "scrape": lambda termino, headless: santaisabel_scraper.scrape(termino, max_pages=5, pause=1.2, headless=headless),
        "subir": santaisabel_scraper.subir_a_supabase,
    },
    "acuenta": {
        "nombre": "A Cuenta",
        "scrape": lambda termino, headless: acuenta_scraper.scrape(termino, pause=1.0, max_pages=5, headless=headless),
        "subir": acuenta_scraper.subir_a_supabase,
    },
    "tottus": {
        "nombre": "Tottus",
        # Solo la primera pagina, ordenada por precio ascendente: menos
        # requests, menos chance de gatillar el desafio de Cloudflare.
        # Con --visible (alguien mirando) espera 180s por si hay que marcar
        # la casilla a mano; sin --visible (automatico, nadie mirando)
        # falla rapido en 15s en vez de esperar en vano.
        "scrape": lambda termino, headless: tottus_scraper.scrape(
            termino, max_pages=1, pause=1.0, headless=headless,
            cloudflare_timeout=(15 if headless else 180),
        ),
        "subir": tottus_scraper.subir_a_supabase,
    },
}

# Santa Isabel y Tottus parecen bloquear las IPs de datacenter de GitHub
# Actions (funcionan perfecto desde una compu normal, pero fallan siempre
# desde ahi). Hasta que encontremos una forma de sortear eso, el workflow
# automatico de cada noche corre solo con las 3 tiendas que si funcionan
# de forma confiable. Las otras dos siguen disponibles para correr a mano
# con --tiendas santaisabel / --tiendas tottus.
TIENDAS_AUTOMATICAS = ["jumbo", "lider", "acuenta"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Corre los scrapers de supermercados para toda la canasta basica")
    parser.add_argument("--limite", type=int, default=None, help="Solo procesar los primeros N terminos (para pruebas)")
    parser.add_argument(
        "--tiendas", nargs="+", choices=list(TIENDAS.keys()), default=TIENDAS_AUTOMATICAS,
        help="Que tiendas correr (por defecto, solo las 3 confiables: jumbo lider acuenta)",
    )
    parser.add_argument("--pausa-min", type=float, default=4.0, help="Pausa minima entre productos, en segundos")
    parser.add_argument("--pausa-max", type=float, default=9.0, help="Pausa maxima entre productos, en segundos")
    parser.add_argument(
        "--visible", action="store_true",
        help="Muestra el navegador (para poder resolver un check de Cloudflare a mano, por ejemplo)",
    )
    args = parser.parse_args()

    terminos = CANASTA_BASICA[: args.limite] if args.limite else CANASTA_BASICA
    tiendas_activas = [TIENDAS[t] for t in args.tiendas]
    headless = not args.visible

    exitos = 0
    intentos = 0
    fallos: list[str] = []

    for i, item in enumerate(terminos, start=1):
        termino = item["termino_busqueda"]
        print(f"\n[{i}/{len(terminos)}] {termino} ({item['nombre_ine']})")

        for tienda in tiendas_activas:
            intentos += 1
            try:
                resultado = tienda["scrape"](termino, headless)
                insertados = tienda["subir"](resultado)
                print(f"  {tienda['nombre']}: {resultado['cantidad_productos']} productos, {insertados} subidas.")
                exitos += 1
            except Exception as exc:
                print(f"  {tienda['nombre']}: ERROR - {exc}", file=sys.stderr)
                fallos.append(f"{tienda['nombre']}/{termino}")
            time.sleep(random.uniform(args.pausa_min / 2, args.pausa_max / 2))

        if i < len(terminos):
            time.sleep(random.uniform(args.pausa_min, args.pausa_max))

    print(f"\nListo. {exitos}/{intentos} combinaciones tienda-producto procesadas sin error.")
    if fallos:
        print(f"Fallaron: {', '.join(fallos)}")

    # No se hace fallar el workflow completo por un puñado de casos con error
    # puntual (un producto agotado, un timeout de red, etc.) - solo si TODO
    # fallo, lo cual indica un problema real (ej: bloqueo del scraping).
    return 1 if exitos == 0 and intentos else 0


if __name__ == "__main__":
    raise SystemExit(main())
