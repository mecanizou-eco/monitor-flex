#!/usr/bin/env python3
"""
Sincroniza os casos auditados recentes (Redshift, via Metabase) para o
Supabase (tabela public.audit_cases), para alimentar o app de login em
tempo real.

Aplica as mesmas correções de qualidade usadas nas questions do Metabase
(coleção "Auditoria"):
  - Deduplica por conversation_sid, mantendo só a task mais recente.
  - Exclui conversas sem demanda real (mensagem automática / saudação
    automática / "Obrigado" isolado) — ver rubrica_auditoria.md.

Variáveis lidas do .env.local (ou do ambiente, no GitHub Actions):
    METABASE_URL, METABASE_API_KEY   — já usados pelos outros scripts
    METABASE_REDSHIFT_DB             — (opcional) padrão 8
    SUPABASE_URL                     — URL do projeto Supabase
    SUPABASE_SERVICE_ROLE_KEY        — chave service_role (nunca a anon)
    SYNC_WINDOW_DAYS                 — (opcional) padrão 10

Uso:
    python3 scripts/sync_supabase.py
    python3 scripts/sync_supabase.py --window-days 45   # backfill maior
    python3 scripts/sync_supabase.py --dry-run
"""

import argparse
import os
import sys
from pathlib import Path

import requests
from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env.local"

DEFAULT_REDSHIFT_DB = 8
DEFAULT_WINDOW_DAYS = 10
PAGE_SIZE = 2000  # limite silencioso do /api/dataset do Metabase — sempre paginar
MAX_PAGES = 100

EXCLUDED_ANALISTAS_SQL = """(
    responsavel_atendimento ILIKE '%renata.santana%'
    OR responsavel_atendimento ILIKE '%thais.abreu%'
    OR responsavel_atendimento ILIKE '%gabriela.rachel%'
    OR responsavel_atendimento ILIKE '%andre.lopes%'
    OR responsavel_atendimento ILIKE '%bruno.ferreira%'
)"""

REDSHIFT_SQL = f"""
WITH audited_raw AS (
    SELECT
        task_sid, conversation_sid, completed_at, channel_type, responsavel_atendimento,
        audit_score, audit_classification,
        nota_e0, nota_e1, nota_e2, nota_e3, nota_e4, nota_e5
    FROM public_facts.ft_conversation_audit
    WHERE completed_at >= CURRENT_DATE - INTERVAL '{{window_days}} day'
      AND responsavel_atendimento IS NOT NULL
    -- Repare: NÃO excluímos logins de escalonamento (Renata etc.) aqui.
    -- Se excluíssemos antes do dedup, uma conversa transferida para Renata
    -- (a task mais recente/completa) perderia essa task da disputa e a task
    -- ANTERIOR, incompleta, de outro analista, "venceria" o dedup por
    -- eliminação — culpando o analista errado por uma conversa que na
    -- verdade foi resolvida corretamente por escalonamento. A exclusão só
    -- é aplicada depois, com base em quem é o vencedor real (ver winner_is_excluded).
),
audited AS (
    SELECT
        a.*,
        JSON_SERIALIZE(ca.problemas)  AS problemas,
        JSON_SERIALIZE(ca.virtudes)   AS virtudes,
        ca.historico_task            AS resumo,
        ca.sugestoes_analista,
        ca.sugestoes_airton,
        ca.observacoes,
        ROW_NUMBER() OVER (PARTITION BY a.conversation_sid ORDER BY a.completed_at DESC) AS rn
    FROM audited_raw a
        LEFT JOIN twilio.conversation_analysis ca USING (task_sid)
    WHERE (ca.historico_task IS NULL
        OR (ca.historico_task NOT ILIKE '%mensagem automática%'
            AND ca.historico_task NOT ILIKE '%resposta automática%'
            AND ca.historico_task NOT ILIKE '%não está atendendo no momento%'
            AND ca.historico_task NOT ILIKE '%horário de funcionamento%'
            AND ca.historico_task NOT ILIKE '%enviou apenas ''Obrigado''%'))
),
flagged AS (
    SELECT
        *,
        MAX(CASE WHEN rn = 1 AND {EXCLUDED_ANALISTAS_SQL} THEN 1 ELSE 0 END)
            OVER (PARTITION BY conversation_sid) AS winner_is_excluded
    FROM audited
),
filtered AS (
    SELECT
        task_sid, conversation_sid, completed_at::varchar AS completed_at, channel_type AS channel,
        responsavel_atendimento AS analista, audit_score AS score, audit_classification AS classificacao,
        nota_e0, nota_e1, nota_e2, nota_e3, nota_e4, nota_e5,
        problemas, virtudes, resumo, sugestoes_analista, sugestoes_airton, observacoes,
        rn, winner_is_excluded
    FROM flagged
    -- Sem "WHERE rn = 1" aqui: mantemos TODAS as tasks (vencedora e
    -- superadas) para que o Python separe quem sincroniza (rn=1 e
    -- winner_is_excluded=0) de quem precisa ser removido/nunca sincronizado
    -- (rn>1 OU winner_is_excluded=1) — ver cleanup_superseded_tasks.
)
SELECT * FROM filtered
ORDER BY completed_at
LIMIT {{limit}} OFFSET {{offset}}
"""


def load_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        env = dict(dotenv_values(ENV_PATH))
    for k in ("METABASE_URL", "METABASE_API_KEY", "METABASE_REDSHIFT_DB",
              "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SYNC_WINDOW_DAYS"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def require(env, keys, context):
    missing = [k for k in keys if not env.get(k)]
    if missing:
        print(f"[ERRO] Para {context}, faltam no .env.local: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)


def fetch_redshift_rows(env, window_days: int) -> list:
    url = env["METABASE_URL"].rstrip("/")
    key = env["METABASE_API_KEY"]
    db = int(env.get("METABASE_REDSHIFT_DB", DEFAULT_REDSHIFT_DB))

    all_rows, cols = [], None
    for page in range(MAX_PAGES):
        sql = REDSHIFT_SQL.format(window_days=window_days, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
        resp = requests.post(
            f"{url}/api/dataset",
            headers={"x-api-key": key, "Content-Type": "application/json"},
            json={"database": db, "type": "native", "native": {"query": sql}},
            timeout=180,
        )
        if resp.status_code not in (200, 202):
            print(f"[ERRO] Metabase/Redshift {resp.status_code}: {resp.text[:400]}", file=sys.stderr)
            break
        payload = resp.json()
        if payload.get("error"):
            print(f"[ERRO] Metabase query error: {payload['error']}", file=sys.stderr)
            break
        data = payload.get("data", {})
        if cols is None:
            cols = [c["name"] for c in data.get("cols", [])]
        rows = data.get("rows", [])
        all_rows.extend(rows)
        print(f"[info] Redshift: página {page + 1} — {len(rows)} registros (acumulado: {len(all_rows)}).",
              file=sys.stderr)
        if len(rows) < PAGE_SIZE:
            break
    else:
        print(f"[aviso] Atingiu o limite de segurança de {MAX_PAGES} páginas.", file=sys.stderr)

    return [dict(zip(cols, row)) for row in all_rows] if cols else []


def upsert_supabase(env, rows: list, batch_size: int = 500):
    url = env["SUPABASE_URL"].rstrip("/")
    key = env["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    sent = 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        resp = requests.post(f"{url}/rest/v1/audit_cases", headers=headers, json=chunk, timeout=60)
        if resp.status_code not in (200, 201, 204):
            print(f"[ERRO] Supabase upsert (lote {i}): {resp.status_code} {resp.text[:400]}", file=sys.stderr)
            sys.exit(1)
        sent += len(chunk)
        print(f"[info] Supabase: lote {i}-{i + len(chunk)} enviado (acumulado: {sent}).", file=sys.stderr)
    return sent


def cleanup_superseded_tasks(env, task_sids: list):
    """Remove do Supabase tasks que já foram superadas por uma task mais
    recente da mesma conversation_sid (rn > 1 na consulta ao Redshift) —
    sem isso o upsert nunca as apaga, e a task parcial/errada antiga fica
    acumulada ao lado da correta para sempre."""
    if not task_sids:
        return 0
    url = env["SUPABASE_URL"].rstrip("/")
    key = env["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    removed = 0
    batch_size = 100  # mantém a URL do filtro "in.(...)" num tamanho razoável
    for i in range(0, len(task_sids), batch_size):
        chunk = task_sids[i:i + batch_size]
        in_filter = "(" + ",".join(chunk) + ")"
        resp = requests.delete(
            f"{url}/rest/v1/audit_cases",
            headers=headers,
            params={"task_sid": f"in.{in_filter}"},
            timeout=60,
        )
        if resp.status_code not in (200, 204):
            print(f"[ERRO] Supabase cleanup (lote {i}): {resp.status_code} {resp.text[:400]}", file=sys.stderr)
            continue
        removed += len(chunk)
    return removed


def main():
    parser = argparse.ArgumentParser(description="Sincroniza casos auditados recentes para o Supabase")
    parser.add_argument("--window-days", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    env = load_env()
    require(env, ["METABASE_URL", "METABASE_API_KEY"], "consultar o Redshift")

    window_days = args.window_days or int(env.get("SYNC_WINDOW_DAYS", DEFAULT_WINDOW_DAYS))
    print(f"[info] Buscando casos dos últimos {window_days} dias…")
    all_rows = fetch_redshift_rows(env, window_days)

    def is_winner(r):
        return r.get("rn") == 1 and not r.get("winner_is_excluded")

    winners = [r for r in all_rows if is_winner(r)]
    losers = [r for r in all_rows if not is_winner(r)]
    for r in winners:
        r.pop("rn", None)
        r.pop("winner_is_excluded", None)
    loser_task_sids = [r["task_sid"] for r in losers]

    print(f"[info] {len(winners)} registros prontos para sincronizar "
          f"({len(losers)} task(s) superada(s) por transferência, a remover).")

    if args.dry_run:
        print("[dry-run] Nada enviado ao Supabase.")
        return

    if not winners:
        print("[aviso] Nenhum registro para sincronizar.", file=sys.stderr)
        return

    require(env, ["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"], "sincronizar com o Supabase")
    sent = upsert_supabase(env, winners)
    print(f"[ok] {sent} registros sincronizados com o Supabase.")

    if loser_task_sids:
        removed = cleanup_superseded_tasks(env, loser_task_sids)
        print(f"[ok] {removed} task(s) superada(s) removida(s) do Supabase.")


if __name__ == "__main__":
    main()
