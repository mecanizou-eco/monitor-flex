#!/usr/bin/env python3
"""
Gerador de PDFs de Auditoria — Mecanizou
Lê a aba Auditorias da planilha Google, agrupa por analista,
gera um PDF por analista e envia para Thais Abreu no Slack.

Uso:
    python3 scripts/generate_audit_pdfs.py
    python3 scripts/generate_audit_pdfs.py --dry-run   # gera PDFs mas não envia
    python3 scripts/generate_audit_pdfs.py --tab "Auditorias"
    python3 scripts/generate_audit_pdfs.py --recipient U03DL2B82KY

Variáveis lidas do .env.local:
    GOOGLE_SHEET_ID     — ID ou URL da planilha
    GOOGLE_SA_FILE      — (opcional) caminho do JSON da conta de serviço
    SLACK_BOT_TOKEN     — token do Slack
"""

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env.local"

BRT = timezone(timedelta(hours=-3))

DEFAULT_RECIPIENT = "U03DL2B82KY"   # Thais Abreu
DEFAULT_TAB       = "Auditorias"

# Analistas excluídos dos PDFs (engenheiros, contas internas)
EXCLUDED_ANALYSTS = {"engineers", "renata.santana"}

# Mapeamento de código de virtude → descrição legível
VIRTUDE_DESC = {
    "V1": "Empatia e acolhimento",
    "V2": "Clareza e objetividade",
    "V3": "Proatividade",
    "V4": "Domínio técnico",
    "V5": "Follow-up eficaz",
    "V6": "Encerramento adequado",
    "V7": "Personalização do atendimento",
    "V8": "Agilidade",
}


# ---------------------------------------------------------------------------
# Env / helpers
# ---------------------------------------------------------------------------

def load_env() -> dict:
    env = dict(dotenv_values(ENV_PATH)) if ENV_PATH.exists() else {}
    for k in ("GOOGLE_SHEET_ID", "GOOGLE_SA_FILE", "SLACK_BOT_TOKEN"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def _sheet_id(raw: str) -> str:
    """Extrai o ID puro da planilha de uma URL ou devolve o valor como está."""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", raw or "")
    return m.group(1) if m else raw


def decode_analyst(raw: str) -> str:
    """Decodifica nome codificado em URL parcial: italo_2Eluiz → Italo Luiz"""
    name = (raw or "").replace("_2E", ".").replace("_2B", "+").replace("_", " ")
    return name.title()


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

def get_google_token(env: dict) -> str:
    import google.auth
    import google.auth.transport.requests
    from google.oauth2 import service_account

    sa_file = env.get("GOOGLE_SA_FILE") or ""
    if not sa_file:
        # Tenta achar na raiz do projeto
        candidates = list(BASE_DIR.glob("mecanizou-*.json")) + list(BASE_DIR.glob("*service-account*.json"))
        if candidates:
            sa_file = str(candidates[0])
    if not sa_file or not Path(sa_file).exists():
        raise RuntimeError("GOOGLE_SA_FILE não encontrado. Configure no .env.local.")

    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds  = service_account.Credentials.from_service_account_file(sa_file, scopes=scopes)
    req    = google.auth.transport.requests.Request()
    creds.refresh(req)
    return creds.token


def read_sheet_rows(env: dict, tab: str) -> list[dict]:
    """Retorna lista de dicts {coluna: valor} a partir da aba da planilha."""
    sheet_id = _sheet_id(env.get("GOOGLE_SHEET_ID") or "")
    if not sheet_id:
        raise RuntimeError("GOOGLE_SHEET_ID não configurado no .env.local.")

    token   = get_google_token(env)
    range_  = urllib.request.quote(f"{tab}!A:Z")
    url     = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_}"
    req     = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        data = json.load(resp)

    values = data.get("values", [])
    if not values:
        return []

    headers = [h.strip() for h in values[0]]
    rows = []
    for row in values[1:]:
        # Preenche colunas faltantes com string vazia
        padded = row + [""] * (len(headers) - len(row))
        rows.append(dict(zip(headers, padded)))
    return rows


# ---------------------------------------------------------------------------
# Group by analyst
# ---------------------------------------------------------------------------

def group_by_analyst(rows: list[dict]) -> dict[str, list[dict]]:
    """Agrupa linhas por analista (coluna responsavel_atendimento)."""
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        # Tenta nomes comuns da coluna
        analyst_raw = (
            row.get("responsavel_atendimento")
            or row.get("responsavel")
            or row.get("atendente")
            or ""
        ).strip()
        if not analyst_raw:
            continue
        slug = analyst_raw.lower().replace(" ", "_")
        if slug in EXCLUDED_ANALYSTS:
            continue
        grouped.setdefault(analyst_raw, []).append(row)
    return grouped


# ---------------------------------------------------------------------------
# PDF generation
# ---------------------------------------------------------------------------

def _virtudes_desc(raw: str) -> list[str]:
    """Converte 'V1, V3' → ['Empatia e acolhimento', 'Proatividade']."""
    codes = [c.strip().upper() for c in re.split(r"[,|;]+", raw or "") if c.strip()]
    return [VIRTUDE_DESC.get(c, c) for c in codes if c]


def _split_pipe(raw: str) -> list[str]:
    """Divide por ' | ' preservando pipes escapados."""
    PLACEHOLDER = "\x00"
    safe = raw.replace("\\|", PLACEHOLDER)
    parts = [p.replace(PLACEHOLDER, "|").strip() for p in safe.split("|")]
    return [p for p in parts if p]


def build_pdf(analyst_raw: str, rows: list[dict], output_path: str) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )

    analyst_name = decode_analyst(analyst_raw)
    today_str    = datetime.now(BRT).strftime("%d/%m/%Y")

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2*cm, bottomMargin=2*cm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleStyle", parent=styles["Title"],
        fontSize=16, spaceAfter=4,
    )
    subtitle_style = ParagraphStyle(
        "SubtitleStyle", parent=styles["Normal"],
        fontSize=10, textColor=colors.grey, spaceAfter=12,
    )
    h2_style = ParagraphStyle(
        "H2Style", parent=styles["Heading2"],
        fontSize=12, spaceBefore=14, spaceAfter=4,
    )
    h3_style = ParagraphStyle(
        "H3Style", parent=styles["Heading3"],
        fontSize=10, spaceBefore=8, spaceAfter=2,
        textColor=colors.HexColor("#333333"),
    )
    body_style = ParagraphStyle(
        "BodyStyle", parent=styles["Normal"],
        fontSize=9, leading=13,
    )
    bullet_style = ParagraphStyle(
        "BulletStyle", parent=styles["Normal"],
        fontSize=9, leading=13, leftIndent=12,
        bulletIndent=0,
    )

    story = []

    # ── Cabeçalho ──────────────────────────────────────────────────────────
    story.append(Paragraph(f"Auditoria de Atendimentos", title_style))
    story.append(Paragraph(f"{analyst_name} · {today_str}", subtitle_style))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#CCCCCC")))
    story.append(Spacer(1, 0.4*cm))

    # ── Resumo Geral ────────────────────────────────────────────────────────
    scores = []
    for r in rows:
        try:
            scores.append(float((r.get("score") or "0").replace(",", ".")))
        except ValueError:
            pass
    media = round(sum(scores) / len(scores), 1) if scores else 0.0

    classificacoes = [r.get("classificacao") or r.get("Classificação") or "" for r in rows]
    excelentes  = sum(1 for c in classificacoes if "excelente" in c.lower())
    bons        = sum(1 for c in classificacoes if "bom" in c.lower() or "boa" in c.lower())
    regulares   = sum(1 for c in classificacoes if "regular" in c.lower())
    ruins       = sum(1 for c in classificacoes if "ruim" in c.lower() or "insatisfat" in c.lower())

    resumo_data = [
        ["Atendimentos auditados", str(len(rows))],
        ["Nota média", f"{str(media).replace('.', ',')} / 100"],
        ["Excelentes", str(excelentes)],
        ["Bons", str(bons)],
        ["Regulares", str(regulares)],
        ["Ruins / Insatisfatórios", str(ruins)],
    ]
    resumo_table = Table(resumo_data, colWidths=[7*cm, 4*cm])
    resumo_table.setStyle(TableStyle([
        ("FONTNAME",   (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE",   (0, 0), (-1, -1), 9),
        ("FONTNAME",   (0, 0), (0, -1), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.HexColor("#F5F5F5"), colors.white]),
        ("GRID",       (0, 0), (-1, -1), 0.3, colors.HexColor("#DDDDDD")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(Paragraph("Resumo Geral", h2_style))
    story.append(resumo_table)
    story.append(Spacer(1, 0.5*cm))

    # ── Detalhes por atendimento ────────────────────────────────────────────
    story.append(Paragraph("Detalhes por Atendimento", h2_style))

    for i, row in enumerate(rows, 1):
        score  = row.get("score") or "—"
        classe = row.get("classificacao") or row.get("Classificação") or "—"
        data_r = row.get("data") or row.get("data_dia") or "—"
        sid    = row.get("conversation_sid") or "—"
        resumo = (row.get("historico_task") or row.get("Histórico Task") or "").strip()
        sugest = (row.get("sugestoes_melhoria") or row.get("Sugestões de Melhoria") or "").strip()
        virtu  = (row.get("virtudes_padronizadas") or row.get("Virtudes Padronizadas") or "").strip()
        probl  = (row.get("problemas_padronizados") or row.get("Problemas Padronizados") or "").strip()

        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#EEEEEE")))
        story.append(Paragraph(
            f"Atendimento {i} — Nota: <b>{score}</b> ({classe}) · {data_r}",
            h3_style,
        ))

        if resumo:
            story.append(Paragraph("<b>Resumo:</b>", body_style))
            story.append(Paragraph(resumo, body_style))
            story.append(Spacer(1, 0.2*cm))

        # Pontos positivos
        vlist = _virtudes_desc(virtu)
        if vlist:
            story.append(Paragraph("<b>Pontos positivos:</b>", body_style))
            for v in vlist:
                story.append(Paragraph(f"• {v}", bullet_style))
            story.append(Spacer(1, 0.2*cm))

        # Oportunidades de melhoria
        olist = _split_pipe(sugest) if sugest else []
        if olist:
            story.append(Paragraph("<b>Oportunidades de melhoria:</b>", body_style))
            for o in olist:
                story.append(Paragraph(f"• {o}", bullet_style))
            story.append(Spacer(1, 0.2*cm))

        # Problemas padronizados
        if probl:
            story.append(Paragraph(f"<b>Problemas:</b> {probl}", body_style))
            story.append(Spacer(1, 0.1*cm))

    story.append(Spacer(1, 0.5*cm))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#CCCCCC")))

    doc.build(story)


# ---------------------------------------------------------------------------
# Slack upload
# ---------------------------------------------------------------------------

def slack_upload_pdf(token: str, user_id: str, file_path: str, analyst_name: str) -> None:
    """Upload moderno do Slack: getUploadURLExternal → upload → completeUpload."""
    filename = Path(file_path).name
    filesize = Path(file_path).stat().st_size

    # 1) Pede a URL de upload
    params = urllib.parse.urlencode({
        "filename": filename,
        "length":   filesize,
    }).encode()
    req1 = urllib.request.Request(
        "https://slack.com/api/files.getUploadURLExternal",
        data=params,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req1) as r:
        resp1 = json.load(r)
    if not resp1.get("ok"):
        raise RuntimeError(f"getUploadURLExternal falhou: {resp1.get('error')}")
    upload_url = resp1["upload_url"]
    file_id    = resp1["file_id"]

    # 2) Faz o upload dos bytes
    with open(file_path, "rb") as f:
        data = f.read()
    req2 = urllib.request.Request(
        upload_url, data=data,
        headers={"Content-Type": "application/pdf"},
        method="POST",
    )
    with urllib.request.urlopen(req2) as r:
        pass  # Slack retorna 200 sem corpo

    # 3) Completa e associa ao canal/usuário
    today_str = datetime.now(BRT).strftime("%d/%m/%Y")
    body = json.dumps({
        "files":           [{"id": file_id}],
        "channel_id":      user_id,
        "initial_comment": f"Auditoria de atendimentos — {analyst_name} · {today_str}",
    }).encode()
    req3 = urllib.request.Request(
        "https://slack.com/api/files.completeUploadExternal",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json; charset=utf-8",
        },
    )
    with urllib.request.urlopen(req3) as r:
        resp3 = json.load(r)
    if not resp3.get("ok"):
        raise RuntimeError(f"completeUploadExternal falhou: {resp3.get('error')}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import urllib.parse  # necessário para slack_upload_pdf

    parser = argparse.ArgumentParser(description="Gera PDFs de auditoria e envia para o Slack")
    parser.add_argument("--dry-run", action="store_true",
                        help="Gera os PDFs mas não envia para o Slack")
    parser.add_argument("--tab", default=None,
                        help=f"Aba da planilha (padrão: AUDIT_SHEET_TAB do .env.local ou '{DEFAULT_TAB}')")
    parser.add_argument("--recipient", default=DEFAULT_RECIPIENT,
                        help=f"User ID do Slack (padrão: {DEFAULT_RECIPIENT})")
    parser.add_argument("--output-dir", default=None,
                        help="Diretório para salvar os PDFs (padrão: diretório temporário)")
    args = parser.parse_args()

    env = load_env()

    # Prioridade: --tab > AUDIT_SHEET_TAB do env > DEFAULT_TAB
    tab = args.tab or env.get("AUDIT_SHEET_TAB") or DEFAULT_TAB

    print(f"Lendo planilha (aba: {tab})...", file=sys.stderr)
    rows = read_sheet_rows(env, tab)
    if not rows:
        print("Planilha vazia ou sem dados.", file=sys.stderr)
        sys.exit(0)

    grouped = group_by_analyst(rows)
    if not grouped:
        print("Nenhum analista encontrado.", file=sys.stderr)
        sys.exit(0)

    print(f"Analistas encontrados: {', '.join(grouped.keys())}", file=sys.stderr)

    token = env.get("SLACK_BOT_TOKEN") or ""
    if not token and not args.dry_run:
        print("[ERRO] SLACK_BOT_TOKEN não configurado.", file=sys.stderr)
        sys.exit(1)

    if args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)
        _ctx = type('_Ctx', (), {'__enter__': lambda s: args.output_dir, '__exit__': lambda s,*a: None})()
    else:
        _ctx = tempfile.TemporaryDirectory()

    with _ctx as tmpdir:
        for analyst_raw, analyst_rows in sorted(grouped.items()):
            analyst_name = decode_analyst(analyst_raw)
            pdf_name     = f"[AUDITORIA DE ATENDIMENTOS] {analyst_name}.pdf"
            pdf_path     = os.path.join(tmpdir, pdf_name)

            print(f"  Gerando PDF: {pdf_name} ({len(analyst_rows)} atendimentos)...",
                  file=sys.stderr)
            build_pdf(analyst_raw, analyst_rows, pdf_path)

            if args.dry_run:
                print(f"  [dry-run] PDF gerado em {pdf_path} — não enviado.", file=sys.stderr)
            else:
                print(f"  Enviando para {args.recipient}...", file=sys.stderr)
                try:
                    slack_upload_pdf(token, args.recipient, pdf_path, analyst_name)
                    print(f"  Enviado: {pdf_name}", file=sys.stderr)
                except Exception as e:
                    print(f"  [ERRO] {analyst_name}: {e}", file=sys.stderr)

    print("Concluído.", file=sys.stderr)


if __name__ == "__main__":
    main()
