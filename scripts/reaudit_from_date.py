#!/usr/bin/env python3
"""
Re-audita conversas já auditadas a partir de uma data e SUBSTITUI as linhas
na planilha Google (em vez de acrescentar duplicatas).

Uso:
    python3 scripts/reaudit_from_date.py --from-date 2026-07-21
    python3 scripts/reaudit_from_date.py --from-date 2026-07-21 --dry-run

O script:
1. Lê a planilha e identifica todas as linhas com data_dia >= --from-date.
2. Coleta os conversation_sids dessas linhas.
3. Remove as linhas antigas da planilha (batch delete via batchUpdate).
4. Re-audita cada conversa com a rubrica atualizada.
5. Grava os novos resultados no final da planilha.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env.local"
BRT = timezone(timedelta(hours=-3))

# Importa funções do audit_agent
sys.path.insert(0, str(BASE_DIR / "scripts"))
import audit_agent as aa


def load_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        env = dict(dotenv_values(ENV_PATH))
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "ANTHROPIC_API_KEY",
              "GOOGLE_SHEET_ID", "GOOGLE_SA_FILE", "AUDIT_SHEET_TAB",
              "SLACK_BOT_TOKEN", "TWILIO_SLACK_CHANNEL_ID", "AUDIT_MODEL"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def to_iso(data_dia: str) -> str:
    """02/07/2026 → 2026-07-02"""
    if not data_dia or "/" not in data_dia:
        return data_dia
    parts = data_dia.split("/")
    if len(parts) == 3:
        return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
    return data_dia


def read_all_rows(sheet_id: str, tab: str, token: str):
    """Devolve (header, rows_as_dicts, raw_values)."""
    rng = urllib.parse.quote(f"{tab}!A1:Z2000", safe="")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{rng}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Sheets read {resp.status_code}: {resp.text[:300]}")
    values = resp.json().get("values", [])
    if not values:
        return [], [], []
    header = values[0]
    rows = values[1:]
    dicts = [dict(zip(header, r + [""] * (len(header) - len(r)))) for r in rows]
    return header, dicts, rows


def delete_rows_by_indices(sheet_id: str, sheet_gid: int, token: str, row_indices: list):
    """Remove linhas da planilha via batchUpdate (índices base-0 relativos à aba, sem o cabeçalho offset)."""
    if not row_indices:
        return
    # Converte para índices da grade (linha 1 = cabeçalho = índice 0; dados começam no índice 1)
    # row_indices são os índices do array `rows` (0-based), então a linha na grade = idx + 1
    # Precisamos deletar de baixo para cima para não deslocar índices
    sorted_indices = sorted(row_indices, reverse=True)
    requests_list = []
    for idx in sorted_indices:
        sheet_row = idx + 1  # +1 para pular cabeçalho
        requests_list.append({
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_gid,
                    "dimension": "ROWS",
                    "startIndex": sheet_row,
                    "endIndex": sheet_row + 1,
                }
            }
        })
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}:batchUpdate"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"requests": requests_list},
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Sheets batchUpdate {resp.status_code}: {resp.text[:300]}")
    print(f"  [ok] {len(row_indices)} linhas removidas da planilha.")


def get_sheet_gid(sheet_id: str, tab: str, token: str) -> int:
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                        params={"fields": "sheets.properties"}, timeout=30)
    for s in resp.json().get("sheets", []):
        if s["properties"]["title"] == tab:
            return s["properties"]["sheetId"]
    raise RuntimeError(f"Aba '{tab}' não encontrada.")


def main():
    parser = argparse.ArgumentParser(description="Re-auditoria parcial com substituição na planilha")
    parser.add_argument("--from-date", required=True,
                        help="Data ISO (YYYY-MM-DD) — re-audita conversas a partir deste dia")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simula sem modificar a planilha")
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--batch-size", type=int, default=10,
                        help="Conversas por lote (pausa de 5s entre lotes). Padrão: 10")
    args = parser.parse_args()

    env = load_env()
    aa.require(env, ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
                     "ANTHROPIC_API_KEY", "GOOGLE_SHEET_ID"], "re-auditar")

    sheet_id_raw = env["GOOGLE_SHEET_ID"].strip()
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", sheet_id_raw)
    sheet_id = m.group(1) if m else sheet_id_raw

    tab = env.get("AUDIT_SHEET_TAB") or "base_de_registros"
    model = args.model or env.get("AUDIT_MODEL") or aa.DEFAULT_MODEL
    from_date = args.from_date  # YYYY-MM-DD

    sa_file = aa.find_service_account_file(env)
    token_google = aa.google_access_token(sa_file)
    tab = aa.resolve_tab(sheet_id, tab, token_google)

    print(f"[info] Lendo planilha '{tab}'…")
    header, row_dicts, _ = read_all_rows(sheet_id, tab, token_google)

    # Encontra linhas com data_dia >= from_date
    target_row_indices = []
    target_sids = []
    for i, r in enumerate(row_dicts):
        raw_date = r.get("data_dia", "")
        iso = to_iso(raw_date)
        sid = r.get("conversation_sid", "").strip()
        if iso >= from_date and sid:
            target_row_indices.append(i)
            target_sids.append(sid)

    print(f"[info] Encontradas {len(target_sids)} conversas com data >= {from_date}:")
    by_day = {}
    for r in row_dicts:
        if to_iso(r.get("data_dia", "")) >= from_date:
            d = r.get("data_dia", "?")
            by_day[d] = by_day.get(d, 0) + 1
    for d in sorted(by_day):
        print(f"  {d}: {by_day[d]} conversas")

    if not target_sids:
        print("[info] Nada a re-auditar.")
        return

    if args.dry_run:
        print(f"\n[dry-run] Seriam re-auditadas {len(target_sids)} conversas e {len(target_row_indices)} linhas seriam removidas.")
        print(f"[dry-run] SIDs: {target_sids[:5]}{'...' if len(target_sids)>5 else ''}")
        return

    # --- Passo 1: busca e re-auditoria das conversas ---
    auth = (env["TWILIO_ACCOUNT_SID"], env["TWILIO_AUTH_TOKEN"])
    rubrica = aa.RUBRICA_PATH.read_text(encoding="utf-8")
    evaluations = []
    total = len(target_sids)

    for i, sid in enumerate(target_sids):
        print(f"\n[{i+1}/{total}] Auditando {sid}…")
        try:
            conv = aa.get_conversation(auth, sid)
            participants = aa.fetch_participants(auth, sid)
            roles = aa.roles_from_participants(participants)
            messages = aa.fetch_messages(auth, sid)
            t = aa.build_transcript(conv, messages, roles)
            t["canal"] = aa.canal_from_participants(participants)
            t["responsavel_atendimento"] = aa.atendente_from_participants(participants)
            ev = aa.audit_transcript(env, model, rubrica, t)
            aa.print_evaluation(ev)
            evaluations.append(ev)
        except Exception as e:
            print(f"  [ERRO] {e}", file=sys.stderr)
            continue

        # pausa entre lotes para respeitar rate limit da Anthropic
        if (i + 1) % args.batch_size == 0 and (i + 1) < total:
            print(f"\n  [pausa 5s entre lotes]")
            time.sleep(5)

    if not evaluations:
        print("\n[ERRO] Nenhuma avaliação concluída.", file=sys.stderr)
        sys.exit(1)

    print(f"\n[info] {len(evaluations)} avaliações concluídas. Atualizando planilha…")

    # --- Passo 2: remove linhas antigas ---
    # Refresh token (pode ter expirado em re-auditorias longas)
    token_google = aa.google_access_token(sa_file)
    sheet_gid = get_sheet_gid(sheet_id, tab, token_google)

    # Só remove as linhas que foram re-auditadas com sucesso
    audited_sids = {ev["conversation_sid"] for ev in evaluations}
    rows_to_delete = [i for i, r in enumerate(row_dicts)
                      if r.get("conversation_sid", "") in audited_sids]
    print(f"[info] Removendo {len(rows_to_delete)} linhas antigas…")
    delete_rows_by_indices(sheet_id, sheet_gid, token_google, rows_to_delete)

    # --- Passo 3: grava novas avaliações ---
    # Refresh token novamente após a deleção
    token_google = aa.google_access_token(sa_file)
    tab_usada, updated, novas = aa.write_to_sheet(sheet_id, tab, token_google, evaluations)
    print(f"\n[ok] {updated} linha(s) gravada(s) na planilha (aba '{tab_usada}').")
    if novas:
        print(f"     Colunas novas: {', '.join(novas)}")

    print(f"\n[concluído] {len(evaluations)}/{total} conversas re-auditadas com as novas regras.")


if __name__ == "__main__":
    main()
