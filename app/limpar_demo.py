"""Safe cleanup for generated demo process records in the local SQLite database.

Only IDs matching the exact generator format SIM followed by six digits are removed.
A dated database backup is created before any deletion. Users, access logs, scraper
execution logs, and all other process numbers remain untouched.
"""
from __future__ import annotations

import argparse
import shutil
from datetime import datetime

from . import db


def demo_process_ids(conn) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM processos WHERE numero_processo GLOB 'SIM[0-9][0-9][0-9][0-9][0-9][0-9]'"
    ).fetchall()
    return [row["id"] for row in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove somente registros no formato de IDs gerados pela demo.")
    parser.add_argument("--yes", action="store_true", help="confirma a remoção sem prompt interativo")
    args = parser.parse_args()

    db.init_db()
    with db.get_db() as conn:
        ids = demo_process_ids(conn)
    print(f"Registros de processo no formato de demonstração encontrados: {len(ids)}")
    if not ids:
        print("Nada a remover.")
        return 0
    if not args.yes:
        confirmation = input("Digite REMOVER DEMO para continuar: ").strip()
        if confirmation != "REMOVER DEMO":
            print("Cancelado; nenhum dado foi alterado.")
            return 1

    backup_path = db.DB_PATH.with_name(
        f"{db.DB_PATH.stem}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}{db.DB_PATH.suffix}"
    )
    shutil.copy2(db.DB_PATH, backup_path)
    placeholders = ",".join("?" for _ in ids)
    with db.get_db() as conn:
        conn.execute(f"DELETE FROM historico_processos WHERE processo_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM processos WHERE id IN ({placeholders})", ids)
    print(f"Removidos {len(ids)} processos fictícios e seus históricos.")
    print(f"Backup preservado em: {backup_path}")
    print("Contas, auditoria, execuções do scraper e processos com outros números foram mantidos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
