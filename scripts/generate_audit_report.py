#!/usr/bin/env python3
"""
Gerador do Relatório HTML de Auditoria de Atendimento — Mecanizou

Fontes de dados (dual-source):
  - Google Sheets (planilha histórica) → registros até 31/07/2026  [src="sheet"]
  - Redshift via Metabase API          → registros a partir de 01/08/2026 [src="redshift"]

O HTML resultante é o mesmo arquivo relatorio_auditoria_mecanizou.html.
A linha tracejada nos gráficos marca a transição (01/08) de amostragem para 100% dos atendimentos.

Uso local:
    python3 scripts/generate_audit_report.py
    python3 scripts/generate_audit_report.py --output /tmp/relatorio.html
    python3 scripts/generate_audit_report.py --post-slack
    python3 scripts/generate_audit_report.py --dry-run

Variáveis lidas do .env.local:
    GOOGLE_SHEET_ID       — id da planilha histórica (Sheets)
    GOOGLE_SA_FILE        — (opcional) caminho do JSON da conta de serviço Google
    AUDIT_SHEET_TAB       — (opcional) aba. Padrão: base_de_registros
    METABASE_URL          — URL base do Metabase (ex: https://metabase.mecanizou.com)
    METABASE_API_KEY      — chave de API do Metabase
    METABASE_REDSHIFT_DB  — (opcional) ID do banco Redshift no Metabase. Padrão: 8
    SLACK_BOT_TOKEN       — token do bot (necessário para --post-slack)
    SLACK_REPORT_CHANNEL  — canal/DM de destino do relatório
    TWILIO_SLACK_CHANNEL_ID — canal fallback para envio
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import dotenv_values

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR      = Path(__file__).resolve().parent.parent
ENV_PATH      = BASE_DIR / ".env.local"
HTML_TEMPLATE = BASE_DIR / "relatorio_auditoria_mecanizou.html"
DEFAULT_OUTPUT = BASE_DIR / "relatorio_auditoria_mecanizou.html"
DEFAULT_TAB   = "base_de_registros"

BRT = timezone(timedelta(hours=-3))

# Data de transição: Sheets até esta data (exclusive), Redshift a partir dela
TRANSITION_DATE = "2026-08-01"

# ID do banco Redshift no Metabase (fallback se METABASE_REDSHIFT_DB não estiver no env)
DEFAULT_REDSHIFT_DB = 8

ETAPA_CODES    = ["E0", "E1", "E2", "E3", "E4", "E5"]
ETAPA_MAX_LIST = [10, 15, 20, 20, 25, 10]
ETAPA_NAMES    = [
    "Recebimento/Roteamento",
    "Primeira Resposta",
    "Diagnóstico/Qualificação",
    "Proposta de Solução",
    "Follow-up Proativo",
    "Encerramento",
]

PROB_LABELS = {
    "P1": "Ausência/atraso de follow-up proativo",
    "P2": "Primeira resposta fora do SLA",
    "P3": "Resposta não respondeu à pergunta do cliente",
    "P4": "Duplicidade / transferência indevida",
    "P5": "Negativa sem explicação",
    "P6": "Informação incorreta ao cliente",
    "P7": "Encerramento sem confirmação",
    "P8": "Escalonamento tardio",
    "P9": "Tom inadequado",
    "P10": "Valor/condição não confirmado",
    "P11": "Cotação/pedido duplicado",
}

# Analistas excluídos de qualquer fonte
EXCLUDED_EMAILS = {"renata.santana@mecanizou.com", "renata.santana@mecanizou.com.br"}
EXCLUDED_NAMES  = {"engineers"}


# ---------------------------------------------------------------------------
# Ambiente
# ---------------------------------------------------------------------------

def load_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        env = dict(dotenv_values(ENV_PATH))
    for k in ("GOOGLE_SHEET_ID", "GOOGLE_SA_FILE", "AUDIT_SHEET_TAB",
              "SLACK_BOT_TOKEN", "SLACK_REPORT_CHANNEL", "TWILIO_SLACK_CHANNEL_ID",
              "METABASE_URL", "METABASE_API_KEY", "METABASE_REDSHIFT_DB"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def require(env, keys, context):
    missing = [k for k in keys if not env.get(k)]
    if missing:
        print(f"[ERRO] Para {context}, faltam no .env.local: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)


def find_sa_file(env) -> Path:
    if env.get("GOOGLE_SA_FILE"):
        p = Path(env["GOOGLE_SA_FILE"])
        return p if p.is_absolute() else BASE_DIR / p
    candidates = sorted(BASE_DIR.glob("mecanizou-*.json"))
    if candidates:
        return candidates[0]
    print("[ERRO] Não encontrei o JSON da conta de serviço do Google.", file=sys.stderr)
    sys.exit(1)


def google_token(sa_file: Path) -> str:
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request as GReq
    except ImportError:
        print("[ERRO] google-auth ausente. Rode: pip install -r requirements.txt", file=sys.stderr)
        sys.exit(1)
    creds = service_account.Credentials.from_service_account_file(
        str(sa_file), scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    creds.refresh(GReq())
    return creds.token


# ---------------------------------------------------------------------------
# Helpers de data e nomes
# ---------------------------------------------------------------------------

def to_iso(s: str) -> str:
    """Normaliza para YYYY-MM-DD qualquer formato de data."""
    if not s:
        return ""
    if "/" in s:
        parts = s.split("/")
        if len(parts) == 3:
            return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
    if "T" in s and len(s) >= 10:
        return s[:10]
    if len(s) == 10 and s[4] == "-":
        return s
    return s


def is_valid_iso(d: str) -> bool:
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", d))


def decode_name(slug: str) -> str:
    """Converte italo_2Eluiz → Italo Luiz"""
    s = slug.replace("_2E", ".").replace("_2B", "+")
    parts = s.split(".")
    return " ".join(p.capitalize() for p in parts)


def normalize_analyst(raw: str) -> str:
    """Normaliza nome/email do analista para exibição."""
    if not raw:
        return ""
    if "airton" in raw.lower():
        return "Airton (IA)"
    # Se parece email (tem @), extrai parte antes do @ e formata
    if "@" in raw:
        local = raw.split("@")[0]
        # remove sufixos numéricos do Twilio
        local = re.sub(r"\d+$", "", local)
        parts = re.split(r"[._\-]", local)
        return " ".join(p.capitalize() for p in parts if p)
    return decode_name(raw)


def is_excluded(raw: str) -> bool:
    """True se o analista deve ser excluído do relatório."""
    low = raw.lower().strip()
    if low in EXCLUDED_NAMES:
        return True
    if low in {e.lower() for e in EXCLUDED_EMAILS}:
        return True
    if "renata.santana" in low:
        return True
    return False


# ---------------------------------------------------------------------------
# Gap computation
# ---------------------------------------------------------------------------

def compute_gap_codes(etapas: list) -> list:
    """Gap no formato histórico: ['E1:−5 (Primeira Resposta)', ...]"""
    gaps = []
    for i, (code, mx, nome) in enumerate(zip(ETAPA_CODES, ETAPA_MAX_LIST, ETAPA_NAMES)):
        nota = etapas[i] if i < len(etapas) else 0
        diff = mx - nota
        if diff > 0:
            gaps.append(f"{code}:−{diff} ({nome})")
    return gaps


def compute_gap_descriptive(etapas_notas: list, etapas_detalhe_json: str) -> list:
    """Gap descritivo (Redshift): ['Primeira Resposta: Analista não se apresentou...', ...]

    Usa a justificativa do modelo para cada etapa abaixo do máximo.
    """
    try:
        detalhe = json.loads(etapas_detalhe_json) if etapas_detalhe_json else {}
    except (json.JSONDecodeError, TypeError):
        detalhe = {}

    gaps = []
    for i, (code, mx, nome) in enumerate(zip(ETAPA_CODES, ETAPA_MAX_LIST, ETAPA_NAMES)):
        nota = etapas_notas[i] if i < len(etapas_notas) else 0
        diff = mx - nota
        if diff <= 0:
            continue
        # Tenta justificativa do modelo; fallback para formato código
        just = ""
        etapa_data = detalhe.get(code) or detalhe.get(code.lower()) or {}
        if isinstance(etapa_data, dict):
            just = (etapa_data.get("justificativa") or etapa_data.get("just") or "").strip()
        if just:
            gaps.append(f"{nome}: {just}")
        else:
            gaps.append(f"{nome} (−{diff} pts)")
    return gaps


# ---------------------------------------------------------------------------
# Fonte 1: Google Sheets (histórico até 31/07/2026)
# ---------------------------------------------------------------------------

def read_sheet(sheet_id: str, tab: str, token: str) -> list:
    rng = urllib.parse.quote(f"{tab}!A1:Z2000", safe="")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{rng}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Sheets API {resp.status_code}: {resp.text[:400]}")
    values = resp.json().get("values", [])
    if not values:
        return []
    header = values[0]
    records = []
    for row in values[1:]:
        row_padded = row + [""] * (len(header) - len(row))
        records.append(dict(zip(header, row_padded)))
    return records


AIRTON_KEYWORDS = {"airton", "(ia)", "n1"}

def is_airton_involved(record: dict) -> bool:
    combined = " ".join([
        record.get("observacoes", ""),
        record.get("historico_task", ""),
        record.get("evidencia_texto", ""),
    ]).lower()
    return any(kw in combined for kw in AIRTON_KEYWORDS)


def build_sheet_records(sheet_records: list) -> list:
    """Converte registros da planilha no formato compacto para o HTML.
    Inclui apenas registros com data < TRANSITION_DATE (histórico até 31/07).
    """
    out = []
    for r in sheet_records:
        resp = r.get("responsavel_atendimento", "").strip()
        if is_excluded(resp):
            continue

        score_raw = r.get("score", "").strip()
        try:
            score = int(float(score_raw))
        except (ValueError, TypeError):
            score = 0

        etapas = []
        for e in ["E0", "E1", "E2", "E3", "E4", "E5"]:
            try:
                etapas.append(int(r.get(f"nota_{e}", "0") or "0"))
            except (ValueError, TypeError):
                etapas.append(0)

        probs = [p.strip() for p in r.get("problemas_padronizados", "").split(",") if p.strip()]
        virts = [v.strip() for v in r.get("virtudes_padronizadas", "").split(",") if v.strip()]
        hist  = (r.get("historico_task", "") or "").strip()

        horario = r.get("horario_conversa", "")
        data_conversa = to_iso(r.get("data_dia", "") or horario or r.get("data", ""))

        if not is_valid_iso(data_conversa):
            continue
        # Só histórico (antes da transição para Redshift)
        if data_conversa >= TRANSITION_DATE:
            continue

        nome_analista = normalize_analyst(resp)

        out.append({
            "d":   data_conversa,
            "a":   nome_analista,
            "s":   score,
            "c":   r.get("classificacao", ""),
            "p":   probs,
            "v":   virts,
            "e":   etapas,
            "h":   hist,
            "g":   compute_gap_codes(etapas),   # formato legado: código + delta
            "air": is_airton_involved(r),
            "src": "sheet",
        })
    return out


# ---------------------------------------------------------------------------
# Fonte 2: Redshift via Metabase (a partir de 01/08/2026)
# ---------------------------------------------------------------------------

REDSHIFT_SQL = """
SELECT
    fa.task_sid,
    fa.conversation_sid,
    DATE(fa.completed_at)::varchar          AS data_conversa,
    fa.completed_at::varchar                AS horario_conversa,
    fa.channel_type                         AS canal,
    fa.responsavel_atendimento,
    fa.audit_score::integer                 AS score,
    fa.audit_classification                 AS classificacao,
    fa.nota_e0::integer                     AS nota_e0,
    fa.nota_e1::integer                     AS nota_e1,
    fa.nota_e2::integer                     AS nota_e2,
    fa.nota_e3::integer                     AS nota_e3,
    fa.nota_e4::integer                     AS nota_e4,
    fa.nota_e5::integer                     AS nota_e5,
    fa.num_mensagens::integer               AS num_mensagens,
    ca.historico_task::varchar              AS historico_task,
    ca.problemas::varchar                   AS problemas_json,
    ca.virtudes::varchar                    AS virtudes_json,
    ca.etapas_detalhe::varchar              AS etapas_detalhe
FROM public_facts.ft_conversation_audit fa
LEFT JOIN twilio.conversation_analysis ca USING (task_sid)
WHERE fa.completed_at >= '{from_date}'
  AND fa.responsavel_atendimento NOT ILIKE '%renata.santana%'
ORDER BY fa.completed_at ASC
"""


def read_redshift_via_metabase(env: dict, from_date: str = TRANSITION_DATE) -> list:
    """Lê auditorias do Redshift via Metabase /api/dataset."""
    url = env.get("METABASE_URL", "").rstrip("/")
    key = env.get("METABASE_API_KEY", "")
    db  = int(env.get("METABASE_REDSHIFT_DB", DEFAULT_REDSHIFT_DB))

    if not url or not key:
        print("[aviso] METABASE_URL ou METABASE_API_KEY ausentes — ignorando Redshift.",
              file=sys.stderr)
        return []

    sql = REDSHIFT_SQL.format(from_date=from_date)
    resp = requests.post(
        f"{url}/api/dataset",
        headers={"x-api-key": key, "Content-Type": "application/json"},
        json={"database": db, "type": "native", "native": {"query": sql}},
        timeout=180,
    )
    if resp.status_code != 200:
        print(f"[ERRO] Metabase/Redshift {resp.status_code}: {resp.text[:400]}", file=sys.stderr)
        return []

    payload = resp.json()
    if payload.get("error"):
        print(f"[ERRO] Metabase query error: {payload['error']}", file=sys.stderr)
        return []

    data = payload.get("data", {})
    cols = [c["name"] for c in data.get("cols", [])]
    rows = data.get("rows", [])
    print(f"[info] Redshift: {len(rows)} registros recebidos.", file=sys.stderr)
    return [dict(zip(cols, row)) for row in rows]


def build_redshift_records(raw_rows: list) -> list:
    """Converte linhas brutas do Redshift no formato compacto para o HTML."""
    out = []
    for r in raw_rows:
        resp = (r.get("responsavel_atendimento") or "").strip()
        if is_excluded(resp):
            continue

        data_conversa = to_iso(r.get("data_conversa") or "")
        if not is_valid_iso(data_conversa):
            continue
        # Só registros a partir da transição
        if data_conversa < TRANSITION_DATE:
            continue

        try:
            score = int(r.get("score") or 0)
        except (ValueError, TypeError):
            score = 0

        etapas = []
        for e in ["nota_e0", "nota_e1", "nota_e2", "nota_e3", "nota_e4", "nota_e5"]:
            try:
                etapas.append(int(r.get(e) or 0))
            except (ValueError, TypeError):
                etapas.append(0)

        # Problemas e virtudes vêm como JSON array string: '["P1","P4"]'
        def parse_json_array(s) -> list:
            if not s:
                return []
            try:
                val = json.loads(s)
                return [str(x) for x in val] if isinstance(val, list) else []
            except (json.JSONDecodeError, TypeError):
                # fallback: separado por vírgula
                return [x.strip() for x in str(s).split(",") if x.strip()]

        probs = parse_json_array(r.get("problemas_json"))
        virts = parse_json_array(r.get("virtudes_json"))
        hist  = (r.get("historico_task") or "").strip()
        nome_analista = normalize_analyst(resp)

        gap = compute_gap_descriptive(etapas, r.get("etapas_detalhe") or "")

        out.append({
            "d":   data_conversa,
            "a":   nome_analista,
            "s":   score,
            "c":   r.get("classificacao") or "",
            "p":   probs,
            "v":   virts,
            "e":   etapas,
            "h":   hist,
            "g":   gap,    # formato descritivo: "Etapa: justificativa do modelo"
            "air": "airton" in nome_analista.lower(),
            "src": "redshift",
        })
    return out


# ---------------------------------------------------------------------------
# Injeção no HTML
# ---------------------------------------------------------------------------

PLACEHOLDER_RE  = re.compile(
    r"// RECORDS_PLACEHOLDER_START.*?// RECORDS_PLACEHOLDER_END",
    re.DOTALL,
)
GEN_TS_RE       = re.compile(r'(<span id="gen-ts">)[^<]*(</span>)')
META_UPDATED_RE = re.compile(r'(<span id="meta-updated">)[^<]*(</span>)')
FOOTER_CONV_RE  = re.compile(r'(<span id="footer-conv-count">)[^<]*(</span>)')
FOOTER_GEN_RE   = re.compile(r'(<span id="footer-gen-ts">)[^<]*(</span>)')


def inject_into_html(template_path: Path, records: list, ts: str) -> str:
    """Substitui o bloco de dados e os timestamps no HTML template."""
    html = template_path.read_text(encoding="utf-8")

    compact_json = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    new_block = (
        "// RECORDS_PLACEHOLDER_START\n"
        f"const RAW = {compact_json};\n"
        "// RECORDS_PLACEHOLDER_END"
    )
    html, count = PLACEHOLDER_RE.subn(new_block, html)
    if count == 0:
        print("[aviso] Marcadores RECORDS_PLACEHOLDER_* não encontrados no template.",
              file=sys.stderr)

    data_br = datetime.now(BRT).strftime("%d/%m/%Y")
    html = GEN_TS_RE.sub(rf'\g<1>{ts}\g<2>', html)
    html = META_UPDATED_RE.sub(rf'\g<1>Atualizado em {data_br}\g<2>', html)
    html = FOOTER_CONV_RE.sub(rf'\g<1>{len(records)} conversas\g<2>', html)
    html = FOOTER_GEN_RE.sub(rf'\g<1>{data_br}\g<2>', html)

    return html


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def slack_upload_html(token: str, channel: str, html_path: Path, ts: str):
    content  = html_path.read_bytes()
    filename = f"relatorio_auditoria_{ts.replace('/', '-').replace(' ', '_').replace(':', '')}.html"

    r1 = requests.get(
        "https://slack.com/api/files.getUploadURLExternal",
        headers={"Authorization": f"Bearer {token}"},
        params={"filename": filename, "length": len(content)},
        timeout=30,
    )
    r1j = r1.json()
    if not r1j.get("ok"):
        print(f"[ERRO] Slack getUploadURLExternal: {r1j}", file=sys.stderr)
        return

    r2 = requests.post(r1j["upload_url"], data=content,
                       headers={"Content-Type": "text/html"}, timeout=60)
    if r2.status_code not in (200, 201):
        print(f"[ERRO] Slack upload bytes {r2.status_code}: {r2.text[:200]}", file=sys.stderr)
        return

    r3 = requests.post(
        "https://slack.com/api/files.completeUploadExternal",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "files": [{"id": r1j["file_id"], "title": f"Relatório de Auditoria — {ts}"}],
            "channel_id": channel,
            "initial_comment": (
                f"📊 *Relatório de Auditoria de Atendimento* atualizado em {ts}\n"
                "Abra no navegador para interatividade completa (filtros, gráficos, exportação)."
            ),
        },
        timeout=30,
    )
    r3j = r3.json()
    if r3j.get("ok"):
        print(f"[ok] Relatório HTML enviado ao Slack (file_id={r1j['file_id']}).")
    else:
        print(f"[ERRO] Slack completeUploadExternal: {r3j}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Gerador do Relatório HTML de Auditoria")
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--tab", type=str, default=None)
    parser.add_argument("--post-slack", action="store_true")
    parser.add_argument("--recipient", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-redshift", action="store_true",
                        help="Pula a leitura do Redshift (usa só Sheets)")
    parser.add_argument("--no-sheets", action="store_true",
                        help="Pula a leitura do Sheets (usa só Redshift)")
    args = parser.parse_args()

    env = load_env()
    tab = args.tab or env.get("AUDIT_SHEET_TAB") or DEFAULT_TAB
    out = Path(args.output) if args.output else DEFAULT_OUTPUT

    all_records: list = []

    # ── Fonte 1: Google Sheets (histórico ≤ 31/07) ──────────────────────────
    if not args.no_sheets and env.get("GOOGLE_SHEET_ID"):
        sheet_id = env["GOOGLE_SHEET_ID"].strip()
        m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", sheet_id)
        if m:
            sheet_id = m.group(1)
        try:
            sa_file = find_sa_file(env)
            print(f"[info] Google Sheets: autenticando com {sa_file.name}…")
            token = google_token(sa_file)
            print(f"[info] Google Sheets: lendo aba '{tab}'…")
            sheet_raw = read_sheet(sheet_id, tab, token)
            sheet_records = build_sheet_records(sheet_raw)
            print(f"[info] Google Sheets: {len(sheet_records)} registros (histórico até 31/07).")
            all_records.extend(sheet_records)
        except Exception as e:
            print(f"[aviso] Falha ao ler Sheets: {e}", file=sys.stderr)
    else:
        if not env.get("GOOGLE_SHEET_ID"):
            print("[info] GOOGLE_SHEET_ID ausente — ignorando Sheets.", file=sys.stderr)

    # ── Fonte 2: Redshift via Metabase (≥ 01/08) ────────────────────────────
    if not args.no_redshift:
        print(f"[info] Redshift: consultando via Metabase (a partir de {TRANSITION_DATE})…")
        try:
            redshift_raw = read_redshift_via_metabase(env)
            redshift_records = build_redshift_records(redshift_raw)
            print(f"[info] Redshift: {len(redshift_records)} registros.")
            all_records.extend(redshift_records)
        except Exception as e:
            print(f"[aviso] Falha ao ler Redshift: {e}", file=sys.stderr)

    if not all_records:
        print("[ERRO] Nenhum registro carregado de nenhuma fonte.", file=sys.stderr)
        sys.exit(1)

    # Ordena por data
    all_records.sort(key=lambda r: r["d"])
    ts = datetime.now(BRT).strftime("%d/%m/%Y %H:%M")
    print(f"[info] Total: {len(all_records)} registros prontos para injeção.")

    if args.dry_run:
        scores = [r["s"] for r in all_records if r["s"] > 0]
        sheet_n = sum(1 for r in all_records if r.get("src") == "sheet")
        rs_n    = sum(1 for r in all_records if r.get("src") == "redshift")
        print(f"  Período: {all_records[0]['d']} → {all_records[-1]['d']}")
        print(f"  Sheets: {sheet_n} | Redshift: {rs_n}")
        if scores:
            print(f"  Score: min={min(scores)}, max={max(scores)}, avg={sum(scores)/len(scores):.1f}")
        analistas = sorted({r["a"] for r in all_records})
        print(f"  Analistas: {analistas}")
        print("[dry-run] Nada salvo.")
        return

    if not HTML_TEMPLATE.exists():
        print(f"[ERRO] Template HTML não encontrado: {HTML_TEMPLATE}", file=sys.stderr)
        sys.exit(1)

    print(f"[info] Injetando dados no template…")
    html = inject_into_html(HTML_TEMPLATE, all_records, ts)

    out.write_text(html, encoding="utf-8")
    print(f"[ok] Relatório salvo em {out} ({out.stat().st_size // 1024} KB).")

    if args.post_slack:
        channel = (args.recipient
                   or env.get("SLACK_REPORT_CHANNEL")
                   or env.get("TWILIO_SLACK_CHANNEL_ID"))
        if not channel:
            print("[aviso] Nenhum canal Slack configurado.", file=sys.stderr)
        else:
            require(env, ["SLACK_BOT_TOKEN"], "enviar ao Slack")
            slack_upload_html(env["SLACK_BOT_TOKEN"], channel, out, ts)


if __name__ == "__main__":
    main()
