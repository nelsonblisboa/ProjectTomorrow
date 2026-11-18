"""CLI do scraper: python -m app.scraper_run [--demo] [--processo NUMERO ...]"""
from __future__ import annotations

import argparse
import logging
import os

from .scraper import executar_coleta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def main():
    ap = argparse.ArgumentParser(description="MAG Tracker — consulta individual autorizada")
    ap.add_argument("--demo", action="store_true", help="gera registros fictícios (somente em ambiente de teste opt-in)")
    ap.add_argument("--processo", help="nº do processo a consultar no portal real")
    ap.add_argument("--cpf", help="CPF para consulta combinada")
    ap.add_argument("--nasc", help="data de nascimento (dd/mm/aaaa) para consulta combinada")
    args = ap.parse_args()

    if args.demo:
        if os.environ.get("MAG_DEMO") != "1" or os.environ.get("MAG_ALLOW_DEMO_DATA") != "1":
            ap.error("--demo exige MAG_DEMO=1 e MAG_ALLOW_DEMO_DATA=1; não use com dados reais.")
        resultado = executar_coleta(demo=True)
    elif args.processo:
        alvo = {"numero_processo": args.processo, "cpf": args.cpf, "data_nascimento": args.nasc}
        resultado = executar_coleta(processos_para_consultar=[alvo])
    else:
        ap.error("Informe --processo NUMERO para uma consulta real; nenhum dado fictício é gerado por padrão.")
    print("Resultado:", resultado)


if __name__ == "__main__":
    main()
