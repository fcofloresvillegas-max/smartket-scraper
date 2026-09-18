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
    python run_nightly.py --tiendas lider          (solo Lider)

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

TIENDAS = {
    "jumbo": {
        "nombre": "Jumbo",
        "scrape": lambda termino: jumbo_scraper.scrape(termino, max_pages=5, pause=1.2, headless=True),
        "subir": jumbo_scraper.subir_a_supabase,
    },
    "lider": {
        "nombre": "Lider",
        "scrape": lambda termino: lider_scraper.scrape(termino, pause=1.0),
        "subir": lider_scraper.subir_a_supabase,
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Corre los scrapers de supermercados para toda la canasta basica")
    parser.add_argument("--limite", type=int, default=None, help="Solo procesar los primeros N terminos (para pruebas)")
    parser.add_argument(
        "--tiendas", nargs="+", choices=list(TIENDAS.keys()), default=list(TIENDAS.keys()),
        help="Que tiendas correr (por defecto todas)",
    )
    parser.add_argument("--pausa-min", type=float, default=4.0, help="Pausa minima entre productos, en segundos")
    parser.add_argument("--pausa-max", type=float, default=9.0, help="Pausa maxima entre productos, en segundos")
    args = parser.parse_args()

    terminos = CANASTA_BASICA[: args.limite] if args.limite else CANASTA_BASICA
    tiendas_activas = [TIENDAS[t] for t in args.tiendas]

    exitos = 0
    intentos = 0
    fallos: list[str] = []

    for i, item in enumerate(terminos, start=1):
        termino = item["termino_busqueda"]
        print(f"\n[{i}/{len(terminos)}] {termino} ({item['nombre_ine']})")

        for tienda in tiendas_activas:
            intentos += 1
            try:
                resultado = tienda["scrape"](termino)
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
