#!/usr/bin/env python3
"""
Gerador do Relatório HTML de Auditoria de Atendimento — Mecanizou

Lê todos os registros da planilha Google (aba base_de_registros), injeta o JSON
compacto no template HTML (substituindo os marcadores RECORDS_PLACEHOLDER_START /
RECORDS_PLACEHOLDER_END) e salva o arquivo gerado.

Opcionalmente envia o HTML via Slack (--post-slack) como mensagem de arquivo para
o canal configurado ou para um usuário específico (--recipient).

Uso local:
    python3 scripts/generate_audit_report.py
    python3 scripts/generate_audit_report.py --output /tmp/relatorio.html
    python3 scripts/generate_audit_report.py --post-slack
    python3 scripts/generate_audit_report.py --dry-run   # imprime stats, não salva

No GitHub Actions este script roda logo após audit_agent.py.

Variáveis lidas do .env.local:
    GOOGLE_SHEET_ID    — id da planilha de destino
    GOOGLE_SA_FILE     — (opcional) caminho do JSON da conta de serviço
    AUDIT_SHEET_TAB    — (opcional) aba. Padrão: base_de_registros
    SLACK_BOT_TOKEN    — token do bot (necessário para --post-slack)
    SLACK_REPORT_CHANNEL  — canal/DM de destino do relatório (opcional; padrão = TWILIO_SLACK_CHANNEL_ID)
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

BASE_DIR     = Path(__file__).resolve().parent.parent
ENV_PATH     = BASE_DIR / ".env.local"
HTML_TEMPLATE = BASE_DIR / "relatorio_auditoria_mecanizou.html"
DEFAULT_OUTPUT = BASE_DIR / "relatorio_auditoria_mecanizou.html"
DEFAULT_TAB  = "base_de_registros"

BRT = timezone(timedelta(hours=-3))

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


# ---------------------------------------------------------------------------
# Ambiente
# ---------------------------------------------------------------------------

def load_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        env = dict(dotenv_values(ENV_PATH))
    for k in ("GOOGLE_SHEET_ID", "GOOGLE_SA_FILE", "AUDIT_SHEET_TAB",
              "SLACK_BOT_TOKEN", "SLACK_REPORT_CHANNEL", "TWILIO_SLACK_CHANNEL_ID"):
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
# Leitura da planilha
# ---------------------------------------------------------------------------

def read_sheet(sheet_id: str, tab: str, token: str) -> list:
    """Lê todos os dados da aba e devolve lista de dicts com cabeçalho como chave."""
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


# ---------------------------------------------------------------------------
# Construção dos registros compactos para o HTML
# ---------------------------------------------------------------------------

def decode_name(slug: str) -> str:
    """Converte italo_2Eluiz → Italo Luiz"""
    s = slug.replace("_2E", ".").replace("_2B", "+")
    parts = s.split(".")
    return " ".join(p.capitalize() for p in parts)


def to_iso(data_dia: str) -> str:
    """02/07/2026 → 2026-07-02"""
    if not data_dia or "/" not in data_dia:
        return data_dia
    parts = data_dia.split("/")
    if len(parts) == 3:
        return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
    return data_dia


AIRTON_KEYWORDS = {"airton", "(ia)", "n1"}

def is_airton_involved(record: dict) -> bool:
    combined = " ".join([
        record.get("observacoes", ""),
        record.get("historico_task", ""),
        record.get("evidencia_texto", ""),
    ]).lower()
    return any(kw in combined for kw in AIRTON_KEYWORDS)


def build_raw_records(sheet_records: list) -> list:
    """Converte registros da planilha no formato compacto para o HTML."""
    out = []
    EXCLUDED = {"engineers"}
    for r in sheet_records:
        resp = r.get("responsavel_atendimento", "").strip()
        if resp in EXCLUDED:
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
        hist  = (r.get("historico_task", "") or "").strip()[:120]

        out.append({
            "d":   to_iso(r.get("data_dia", "")),
            "a":   decode_name(resp) if resp else "",
            "s":   score,
            "c":   r.get("classificacao", ""),
            "p":   probs,
            "v":   virts,
            "e":   etapas,
            "h":   hist,
            "air": is_airton_involved(r),
        })
    return out


# ---------------------------------------------------------------------------
# Injeção no HTML
# ---------------------------------------------------------------------------

PLACEHOLDER_RE = re.compile(
    r"// RECORDS_PLACEHOLDER_START.*?// RECORDS_PLACEHOLDER_END",
    re.DOTALL,
)
GEN_TS_RE = re.compile(r'(<span id="gen-ts">)[^<]*(</span>)')


def inject_into_html(template_path: Path, records: list, ts: str) -> str:
    """Substitui o bloco de dados e o timestamp no HTML template."""
    html = template_path.read_text(encoding="utf-8")

    # Monta o bloco de substituição
    compact_json = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    new_block = (
        "// RECORDS_PLACEHOLDER_START\n"
        f"const RAW = {compact_json};\n"
        "// RECORDS_PLACEHOLDER_END"
    )

    html, count = PLACEHOLDER_RE.subn(new_block, html)
    if count == 0:
        print("[aviso] Marcadores RECORDS_PLACEHOLDER_* não encontrados no template. "
              "O HTML pode estar desatualizado.", file=sys.stderr)

    # Atualiza o timestamp
    html = GEN_TS_RE.sub(rf'\g<1>{ts}\g<2>', html)
    return html


# ---------------------------------------------------------------------------
# Slack — envio do arquivo HTML
# ---------------------------------------------------------------------------

def slack_upload_html(token: str, channel: str, html_path: Path, ts: str):
    """Envia o HTML de relatório para um canal/DM do Slack (3-step upload)."""
    content = html_path.read_bytes()
    filename = f"relatorio_auditoria_{ts.replace('/', '-').replace(' ', '_').replace(':', '')}.html"

    # Step 1: getUploadURLExternal
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

    upload_url = r1j["upload_url"]
    file_id    = r1j["file_id"]

    # Step 2: POST bytes
    r2 = requests.post(upload_url, data=content,
                       headers={"Content-Type": "text/html"}, timeout=60)
    if r2.status_code not in (200, 201):
        print(f"[ERRO] Slack upload bytes {r2.status_code}: {r2.text[:200]}", file=sys.stderr)
        return

    # Step 3: completeUploadExternal
    r3 = requests.post(
        "https://slack.com/api/files.completeUploadExternal",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "files": [{"id": file_id, "title": f"Relatório de Auditoria — {ts}"}],
            "channel_id": channel,
            "initial_comment": f"📊 *Relatório de Auditoria de Atendimento* atualizado em {ts}\n"
                               "Abra no navegador para interatividade completa (filtro de data, gráficos).",
        },
        timeout=30,
    )
    r3j = r3.json()
    if r3j.get("ok"):
        print(f"[ok] Relatório HTML enviado ao Slack (file_id={file_id}).")
    else:
        print(f"[ERRO] Slack completeUploadExternal: {r3j}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Gerador do Relatório HTML de Auditoria")
    parser.add_argument("--output", type=str, default=None,
                        help="Caminho do HTML de saída (padrão: relatorio_auditoria_mecanizou.html na raiz)")
    parser.add_argument("--tab", type=str, default=None,
                        help="Aba da planilha (padrão: env AUDIT_SHEET_TAB ou base_de_registros)")
    parser.add_argument("--post-slack", action="store_true",
                        help="Envia o HTML gerado ao Slack")
    parser.add_argument("--recipient", type=str, default=None,
                        help="Canal ou User ID do Slack de destino (sobrepõe SLACK_REPORT_CHANNEL)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Lê a planilha e imprime stats, mas NÃO salva nem envia")
    args = parser.parse_args()

    env  = load_env()
    tab  = args.tab or env.get("AUDIT_SHEET_TAB") or DEFAULT_TAB
    out  = Path(args.output) if args.output else DEFAULT_OUTPUT

    require(env, ["GOOGLE_SHEET_ID"], "ler a planilha Google")
    sheet_id = env["GOOGLE_SHEET_ID"].strip()
    # aceita URL inteira ou ID puro
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", sheet_id)
    if m:
        sheet_id = m.group(1)

    sa_file = find_sa_file(env)
    print(f"[info] Autenticando com {sa_file.name}…")
    token   = google_token(sa_file)

    print(f"[info] Lendo planilha {sheet_id} · aba '{tab}'…")
    sheet_records = read_sheet(sheet_id, tab, token)
    if not sheet_records:
        print("[ERRO] Planilha vazia ou aba não encontrada.", file=sys.stderr)
        sys.exit(1)

    raw = build_raw_records(sheet_records)
    ts  = datetime.now(BRT).strftime("%d/%m/%Y %H:%M")

    print(f"[info] {len(raw)} registros prontos para injeção.")

    if args.dry_run:
        scores = [r["s"] for r in raw if r["s"] > 0]
        print(f"  Período: {min(r['d'] for r in raw)} → {max(r['d'] for r in raw)}")
        print(f"  Score: min={min(scores)}, max={max(scores)}, avg={sum(scores)/len(scores):.1f}")
        analistas = {r["a"] for r in raw}
        print(f"  Analistas: {sorted(analistas)}")
        print("[dry-run] Nada salvo.")
        return

    if not HTML_TEMPLATE.exists():
        print(f"[ERRO] Template HTML não encontrado: {HTML_TEMPLATE}", file=sys.stderr)
        print("  Execute primeiro localmente para gerar o template inicial.", file=sys.stderr)
        sys.exit(1)

    print(f"[info] Injetando dados no template…")
    html = inject_into_html(HTML_TEMPLATE, raw, ts)

    out.write_text(html, encoding="utf-8")
    print(f"[ok] Relatório salvo em {out} ({out.stat().st_size // 1024} KB).")

    if args.post_slack:
        channel = (args.recipient
                   or env.get("SLACK_REPORT_CHANNEL")
                   or env.get("TWILIO_SLACK_CHANNEL_ID"))
        if not channel:
            print("[aviso] Nenhum canal Slack configurado (SLACK_REPORT_CHANNEL ou "
                  "TWILIO_SLACK_CHANNEL_ID). Use --recipient.", file=sys.stderr)
        else:
            require(env, ["SLACK_BOT_TOKEN"], "enviar ao Slack")
            slack_upload_html(env["SLACK_BOT_TOKEN"], channel, out, ts)


if __name__ == "__main__":
    main()
