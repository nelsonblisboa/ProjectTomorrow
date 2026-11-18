"""CLI do scraper: python -m app.scraper_run [--demo] [--processo NUMERO [--cpf ... --nasc ...]]"""
from __future__ import annotations

import argparse
import logging

from .scraper import executar_coleta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def main():
    ap = argparse.ArgumentParser(description="MAG Tracker — coleta automatizada")
    ap.add_argument("--demo", action="store_true", help="popula o banco com dados fictícios (desenvolvimento)")
    ap.add_argument("--processo", help="nº do processo a consultar no portal real")
    ap.add_argument("--cpf", help="CPF para consulta combinada")
    ap.add_argument("--nasc", help="data de nascimento (dd/mm/aaaa) para consulta combinada")
    args = ap.parse_args()

    alvos = []
    if args.processo:
        alvos.append({"numero_processo": args.processo, "cpf": args.cpf, "data_nascimento": args.nasc})

    resultado = executar_coleta(processos_para_consultar=alvos, demo=args.demo or not alvos)
    print("Resultado:", resultado)


if __name__ == "__main__":
    main()
