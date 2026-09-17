#!/usr/bin/env python3
"""
run_nightly.py

Corre el scraper de Jumbo para cada producto de canasta_basica.py y sube
los resultados a Supabase. Pensado para ejecutarse una vez por noche desde
GitHub Actions (ver .github/workflows/nightly.yml), pero tambien se puede
correr a mano para probar.

Uso:
    python run_nightly.py
    python run_nightly.py --limite 5      (solo los primeros 5 terminos, para probar)

Requiere las mismas variables de entorno que jumbo_scraper.py --supabase:
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY
"""

from __future__ import annotations

import argparse
import random
import sys
import time

from canasta_basica import CANASTA_BASICA
from jumbo_scraper import scrape, subir_a_supabase


def main() -> int:
    parser = argparse.ArgumentParser(description="Corre el scraper de Jumbo para toda la canasta basica")
    parser.add_argument("--limite", type=int, default=None, help="Solo procesar los primeros N terminos (para pruebas)")
    parser.add_argument("--pausa-min", type=float, default=4.0, help="Pausa minima entre productos, en segundos")
    parser.add_argument("--pausa-max", type=float, default=9.0, help="Pausa maxima entre productos, en segundos")
    args = parser.parse_args()

    terminos = CANASTA_BASICA[: args.limite] if args.limite else CANASTA_BASICA

    exitos = 0
    fallos: list[str] = []

    for i, item in enumerate(terminos, start=1):
        termino = item["termino_busqueda"]
        print(f"\n[{i}/{len(terminos)}] Buscando: {termino} ({item['nombre_ine']})")
        try:
            resultado = scrape(termino, max_pages=5, pause=1.2, headless=True)
            insertados = subir_a_supabase(resultado)
            print(f"  -> {resultado['cantidad_productos']} productos, {insertados} filas subidas a Supabase.")
            exitos += 1
        except Exception as exc:
            print(f"  -> ERROR con '{termino}': {exc}", file=sys.stderr)
            fallos.append(termino)

        if i < len(terminos):
            time.sleep(random.uniform(args.pausa_min, args.pausa_max))

    print(f"\nListo. {exitos}/{len(terminos)} terminos procesados sin error.")
    if fallos:
        print(f"Fallaron: {', '.join(fallos)}")

    # No se hace fallar el workflow completo por un puñado de terminos con
    # error puntual (un producto agotado, un timeout de red, etc.) - solo
    # si TODO fallo, lo cual indica un problema real (ej: Jumbo bloqueo el
    # scraping, o cambio de estructura de pagina).
    return 1 if exitos == 0 and terminos else 0


if __name__ == "__main__":
    raise SystemExit(main())
