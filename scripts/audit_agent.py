#!/usr/bin/env python3
"""
Agente de Auditoria de Atendimento — Mecanizou

Puxa conversas do Twilio Conversations, pede ao Claude (API da Anthropic) que
avalie cada uma seguindo a `rubrica_auditoria.md`, e grava o resultado numa
planilha do Google (uma linha por conversa auditada).

Fluxo pensado para começar PEQUENO e validar antes de automatizar:

    # 1) Conferir o formato bruto de UMA conversa (sem IA, sem planilha)
    python3 scripts/audit_agent.py --raw --sample 1

    # 2) Auditar 2 conversas e só IMPRIMIR o resultado (não escreve na planilha)
    python3 scripts/audit_agent.py --dry-run --sample 2

    # 3) Auditar uma conversa específica e IMPRIMIR
    python3 scripts/audit_agent.py --dry-run --conversation-sid CHxxxxxxxx

    # 4) Rodar de verdade: audita e grava na planilha Google
    python3 scripts/audit_agent.py --sample 2

    # Validar a lógica de nota offline, sem Twilio (usa um transcript de arquivo)
    python3 scripts/audit_agent.py --dry-run --fixture estudos/exemplo_conversa.json

Variáveis lidas do .env.local (na raiz do projeto):
    TWILIO_ACCOUNT_SID     — já usado pelo monitor
    TWILIO_AUTH_TOKEN      — já usado pelo monitor
    ANTHROPIC_API_KEY      — chave da API da Anthropic (nova)
    GOOGLE_SHEET_ID        — id da planilha de destino (nova)
    GOOGLE_SA_FILE         — (opcional) caminho do JSON da conta de serviço.
                             Se ausente, procura um arquivo mecanizou-*.json na raiz.
    AUDIT_MODEL            — (opcional) modelo Claude. Padrão: claude-sonnet-4-6
    AUDIT_SHEET_TAB        — (opcional) aba da planilha. Padrão: Auditorias
"""

import argparse
import json
import os
import random
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import dotenv_values

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env.local"
RUBRICA_PATH = BASE_DIR / "rubrica_auditoria.md"

CONVERSATIONS_BASE = "https://conversations.twilio.com/v1"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_SHEET_TAB = "Auditorias"

BRT = timezone(timedelta(hours=-3))

# Peso máximo de cada etapa (fonte: rubrica_auditoria.md — total 100)
ETAPA_MAX = {"E0": 10, "E1": 15, "E2": 20, "E3": 20, "E4": 25, "E5": 10}
ETAPA_ORDEM = ["E0", "E1", "E2", "E3", "E4", "E5"]
ETAPA_NOME = {
    "E0": "Recebimento e Roteamento",
    "E1": "Primeira Resposta",
    "E2": "Diagnóstico / Qualificação",
    "E3": "Proposta de Solução e Confirmação",
    "E4": "Follow-up Proativo",
    "E5": "Encerramento e Confirmação",
}

# Colunas canônicas da planilha, na ordem preferida.
# As 10 primeiras são exatamente as que a planilha de auditoria já usa hoje —
# assim o agente PREENCHE as colunas existentes em vez de recriá-las. As demais
# (classificação, resumo inteligente, notas por etapa, etc.) são acrescentadas
# à direita porque a rubrica precisa delas.
CANONICAL_ORDER = [
    "data",
    "horario_conversa",
    "canal",
    "responsavel_atendimento",
    "score",
    "evidencia_texto",
    "problemas_padronizados",
    "virtudes_padronizadas",
    "sugestoes_melhoria",
    "sugestoes_analista",
    "sugestoes_airton",
    "observacoes",
    # --- complementos exigidos pela rubrica ---
    "classificacao",
    "historico_task",
    "conversation_sid",
    "friendly_name",
    "state",
    "num_mensagens",
    "nota_E0",
    "nota_E1",
    "nota_E2",
    "nota_E3",
    "nota_E4",
    "nota_E5",
    "gap_para_100",
    "modelo",
    "feedback_gestora",
]

# Apelidos: cabeçalhos existentes (já normalizados) que devem casar com uma
# coluna canônica mesmo escritos de forma diferente. Evita coluna duplicada.
COLUMN_ALIASES = {
    "dataauditoria": "data",
    "dataavaliacao": "data",
    "datadia": "data",
    "horarioconversa": "horario_conversa",
    "datahoraconversa": "horario_conversa",
    "responsavel": "responsavel_atendimento",
    "atendente": "responsavel_atendimento",
    "nota": "score",
    "notatotal": "score",
    "pontuacao": "score",
    "evidencia": "evidencia_texto",
    "problemas": "problemas_padronizados",
    "problemaspadronizados": "problemas_padronizados",
    "virtudes": "virtudes_padronizadas",
    "virtudespadronizadas": "virtudes_padronizadas",
    "sugestoes": "sugestoes_melhoria",
    "sugestoesmelhoria": "sugestoes_melhoria",
    "obs": "observacoes",
    "classificacao": "classificacao",
    "historico": "historico_task",
    "historicotask": "historico_task",
    "resumo": "historico_task",
    "sid": "conversation_sid",
    "conversationsid": "conversation_sid",
    "estado": "state",
    "nummensagens": "num_mensagens",
    "mensagens": "num_mensagens",
    "modelo": "modelo",
}


def _norm_col(nome: str) -> str:
    """Normaliza um nome de coluna para casar apelidos (sem acento, minúsculo, só alfanumérico)."""
    import unicodedata
    txt = unicodedata.normalize("NFKD", nome or "").encode("ascii", "ignore").decode()
    return "".join(ch for ch in txt.lower() if ch.isalnum())


def canonical_for(header_cell: str):
    """Descobre qual coluna canônica corresponde a um cabeçalho existente (ou None)."""
    n = _norm_col(header_cell)
    for c in CANONICAL_ORDER:
        if _norm_col(c) == n:
            return c
    return COLUMN_ALIASES.get(n)


# ---------------------------------------------------------------------------
# Ambiente
# ---------------------------------------------------------------------------

def load_env() -> dict:
    """Lê .env.local (se existir) e sobrepõe com variáveis de ambiente do sistema.

    Não trava se o arquivo faltar: nesse caso usa só o ambiente (útil no GitHub
    Actions, onde as chaves vêm dos secrets). A checagem do que é obrigatório fica
    por conta de `require()`, com mensagem amigável por operação.
    """
    env = {}
    if ENV_PATH.exists():
        env = dict(dotenv_values(ENV_PATH))
    else:
        print(f"[aviso] não encontrei {ENV_PATH}. Vou usar só as variáveis de "
              f"ambiente do sistema (se houver).", file=sys.stderr)
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "ANTHROPIC_API_KEY",
              "GOOGLE_SHEET_ID", "GOOGLE_SA_FILE", "AUDIT_MODEL", "AUDIT_SHEET_TAB",
              "SLACK_BOT_TOKEN", "TWILIO_SLACK_CHANNEL_ID"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def require(env: dict, keys: list, contexto: str):
    """Interrompe com mensagem amigável se faltar alguma variável."""
    missing = [k for k in keys if not env.get(k)]
    if missing:
        print(f"[ERRO] Para {contexto}, faltam no .env.local: {', '.join(missing)}",
              file=sys.stderr)
        sys.exit(1)


def normalize_sheet_id(raw: str) -> str:
    """Aceita tanto o ID puro da planilha quanto a URL inteira colada do navegador."""
    if not raw:
        return raw
    raw = raw.strip()
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", raw)
    if m:
        return m.group(1)
    return raw


def find_service_account_file(env: dict) -> Path:
    """Descobre o JSON da conta de serviço do Google."""
    if env.get("GOOGLE_SA_FILE"):
        p = Path(env["GOOGLE_SA_FILE"])
        if not p.is_absolute():
            p = BASE_DIR / p
        return p
    candidatos = sorted(BASE_DIR.glob("mecanizou-*.json"))
    if candidatos:
        return candidatos[0]
    print("[ERRO] Não encontrei o JSON da conta de serviço do Google. "
          "Defina GOOGLE_SA_FILE no .env.local ou coloque um mecanizou-*.json na raiz.",
          file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Twilio Conversations — busca
# ---------------------------------------------------------------------------

def tw_get(url: str, auth: tuple, params: dict = None) -> dict:
    resp = requests.get(url, auth=auth, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def list_conversations(auth: tuple, sample: int, state: str) -> list:
    """Lista conversas recentes, ordenadas por data de atualização (desc)."""
    params = {"PageSize": max(sample * 5, 20)}
    if state and state != "all":
        params["State"] = state
    data = tw_get(f"{CONVERSATIONS_BASE}/Conversations", auth, params)
    convs = data.get("conversations", [])
    convs.sort(key=lambda c: c.get("date_updated") or "", reverse=True)
    return convs[:sample]


def _parse_iso(s: str):
    """Parse de timestamp ISO do Twilio (com ou sem fração de segundo)."""
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _conv_date_brt(date_created: str) -> str:
    """Converte date_created (UTC) para ISO em BRT. Fallback: datetime.now(BRT)."""
    ts = _parse_iso(date_created)
    if ts:
        return ts.astimezone(BRT).isoformat()
    return datetime.now(BRT).isoformat()


def conversation_duration_min(conv: dict):
    """Duração aproximada da conversa em minutos (date_updated - date_created)."""
    ini = _parse_iso(conv.get("date_created"))
    fim = _parse_iso(conv.get("date_updated"))
    if ini and fim:
        return max(0.0, (fim - ini).total_seconds() / 60.0)
    return None


def analyst_of(conv: dict):
    """Tenta identificar o analista responsável a partir dos attributes da conversa.

    Em muitas conversas do Twilio Conversations o analista NÃO vem identificado
    (attributes vazio, mensagens do negócio com author = SID da conversa). Quando
    não dá para identificar, retorna None e a estratificação por analista é pulada.
    O vínculo confiável conversa→analista mora no TaskRouter (Tasks atribuídas a
    Workers) — a ligar quando confirmarmos o formato com o time.
    """
    attrs = conv.get("attributes")
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs) if attrs.strip() else {}
        except (ValueError, TypeError):
            return None
    if not isinstance(attrs, dict):
        return None
    for chave in ("worker_name", "workerName", "worker_sid", "workerSid",
                  "agent", "agent_name", "owner", "assignee", "responsavel"):
        val = attrs.get(chave)
        if val:
            return str(val)
    return None


def fetch_pool_metadata(auth: tuple, pool: list) -> dict:
    """Busca participantes de todas as conversas do pool em paralelo.

    Retorna dict sid → {'analyst': str|None, 'airton_only': bool, 'canal': str}.
    Conversas Airton-only são aquelas sem nenhum participante com identity humana
    (apenas identities contendo "airton" ou sem identity alguma — sem analista humano).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def fetch_one(conv):
        sid = conv.get("sid", "")
        try:
            participants = fetch_participants(auth, sid)
            identities = [p.get("identity") for p in participants if p.get("identity")]
            airton_ids = [i for i in identities if "airton" in i.lower()]
            human_ids  = [i for i in identities if "airton" not in i.lower()]
            analyst    = human_ids[0] if human_ids else None
            # Airton-only: há pelo menos 1 participante Airton e nenhum humano
            airton_only = bool(airton_ids) and not bool(human_ids)
            canal = canal_from_participants(participants)
            return sid, {"analyst": analyst, "airton_only": airton_only, "canal": canal}
        except Exception:
            return sid, {"analyst": None, "airton_only": False, "canal": ""}

    results = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(fetch_one, conv): conv for conv in pool}
        for future in as_completed(futures):
            sid, meta = future.result()
            results[sid] = meta
    return results


def select_conversations_for_audit(auth: tuple, limit: int, state: str,
                                   long_min: float = 60.0, long_share: float = 0.7,
                                   seed=None) -> tuple:
    """Seleciona conversas para auditoria com amostragem justa.

    Regras:
      - Só conversas ENCERRADAS por padrão (state='closed').
      - Teto de `limit` conversas por rodada (padrão 10/dia).
      - Ao menos 1 conversa Airton-only (sem analista humano) por rodada.
      - Ao menos 1 conversa por analista humano identificado no pool.
      - Slots restantes preenchem com proporção long/short (~70% longas).
      - Randomização com `seed` para reprodutibilidade.

    Retorna (selecionadas, meta).
    """
    params = {"PageSize": 50}
    if state and state != "all":
        params["State"] = state
    data = tw_get(f"{CONVERSATIONS_BASE}/Conversations", auth, params)
    pool = data.get("conversations", [])
    pool.sort(key=lambda c: c.get("date_updated") or "", reverse=True)

    rng = random.Random(seed)

    # Busca metadados do pool (analistas + Airton-only) em paralelo
    print("[amostra] Classificando pool de conversas (participants)…", file=sys.stderr)
    pool_meta = fetch_pool_metadata(auth, pool)

    # Separa em grupos
    airton_pool  = [c for c in pool if pool_meta.get(c.get("sid"), {}).get("airton_only")]
    by_analyst: dict = {}
    for c in pool:
        sid  = c.get("sid", "")
        meta = pool_meta.get(sid, {})
        if meta.get("airton_only"):
            continue
        a = meta.get("analyst")
        if a:
            by_analyst.setdefault(a, []).append(c)

    selected_sids: set = set()
    selected: list = []

    # 1. Ao menos 1 Airton-only
    if airton_pool:
        pick = rng.choice(airton_pool)
        selected.append(pick)
        selected_sids.add(pick.get("sid"))

    # 2. Ao menos 1 por analista humano (até preencher limit)
    analysts_sorted = sorted(by_analyst.keys(), key=lambda a: rng.random())
    for a in analysts_sorted:
        if len(selected) >= limit:
            break
        candidates = [c for c in by_analyst[a] if c.get("sid") not in selected_sids]
        if candidates:
            pick = rng.choice(candidates)
            selected.append(pick)
            selected_sids.add(pick.get("sid"))

    # 3. Slots restantes: proporção long/short
    remaining_pool = [c for c in pool if c.get("sid") not in selected_sids]
    longs  = [c for c in remaining_pool if (conversation_duration_min(c) or 0) >= long_min]
    shorts = [c for c in remaining_pool if (conversation_duration_min(c) or 0) < long_min]

    faltam = limit - len(selected)
    if faltam > 0:
        n_long = min(len(longs), round(faltam * long_share))
        extra = rng.sample(longs, n_long) if n_long else []
        extra += rng.sample(shorts, min(faltam - len(extra), len(shorts)))
        if len(extra) < faltam:
            resto = [c for c in longs if c not in extra]
            extra += rng.sample(resto, min(faltam - len(extra), len(resto)))
        selected += extra[:faltam]

    rng.shuffle(selected)
    selected = selected[:limit]

    all_analysts = {pool_meta.get(c.get("sid"), {}).get("analyst")
                    for c in selected if not pool_meta.get(c.get("sid"), {}).get("airton_only")}
    analistas = {a for a in all_analysts if a}
    n_airton  = sum(1 for c in selected if pool_meta.get(c.get("sid"), {}).get("airton_only"))

    meta = {
        "pool": len(pool),
        "longas_no_pool": len([c for c in pool if (conversation_duration_min(c) or 0) >= long_min]),
        "curtas_no_pool": len([c for c in pool if (conversation_duration_min(c) or 0) < long_min]),
        "airton_only_no_pool": len(airton_pool),
        "selecionadas": len(selected),
        "analistas_identificados": len(analistas),
        "airton_only_selecionadas": n_airton,
    }
    return selected, meta


def get_conversation(auth: tuple, sid: str) -> dict:
    return tw_get(f"{CONVERSATIONS_BASE}/Conversations/{sid}", auth)


def fetch_participants(auth: tuple, sid: str) -> list:
    """Baixa a lista bruta de participantes de uma conversa."""
    try:
        data = tw_get(f"{CONVERSATIONS_BASE}/Conversations/{sid}/Participants", auth,
                      {"PageSize": 50})
    except Exception as e:
        print(f"  [aviso] não consegui ler participantes de {sid}: {e}", file=sys.stderr)
        return []
    return data.get("participants", [])


def roles_from_participants(participants: list) -> dict:
    """Mapeia autor -> papel (Cliente/Atendente).

    Heurística: participante com messaging_binding (SMS/WhatsApp) = Cliente
    externo; participante com identity (login no Flex/bot) = Atendente.
    """
    roles = {}
    for p in participants:
        binding = p.get("messaging_binding") or {}
        identity = p.get("identity")
        if binding.get("address"):
            roles[binding["address"]] = "Cliente"
        if identity:
            if "airton" in identity.lower():
                roles[identity] = "Airton (IA)"
            else:
                roles[identity] = "Analista"
    return roles


def canal_from_participants(participants: list) -> str:
    """Descobre o canal (whatsapp, sms, ...) a partir do binding do cliente."""
    for p in participants:
        b = p.get("messaging_binding") or {}
        tipo = (b.get("type") or "").strip()
        if tipo:
            return tipo
        addr = b.get("address") or ""
        if addr.startswith("whatsapp:"):
            return "whatsapp"
        if addr:
            return "sms"
    return ""


def atendente_from_participants(participants: list) -> str:
    """Nome/identity do primeiro atendente (participante com identity)."""
    for p in participants:
        if p.get("identity"):
            return p["identity"]
    return ""


def fetch_messages(auth: tuple, sid: str) -> list:
    """Puxa todas as mensagens da conversa em ordem cronológica."""
    msgs = []
    url = f"{CONVERSATIONS_BASE}/Conversations/{sid}/Messages"
    params = {"PageSize": 100, "Order": "asc"}
    while url:
        data = tw_get(url, auth, params)
        msgs.extend(data.get("messages", []))
        next_url = (data.get("meta") or {}).get("next_page_url")
        url = next_url
        params = None  # a next_page_url já vem com os parâmetros
    return msgs


def _looks_like_customer_addr(author: str) -> bool:
    """Heurística de fallback: autores que parecem endereço de canal externo."""
    return author.startswith(("whatsapp:", "sms:", "messenger:", "+"))


def _classify_author(autor: str, conv_sid: str, customer_addrs: set, roles: dict) -> str:
    """Classifica o papel de um autor de mensagem.

    Regras (em ordem de prioridade):
    1. Endereço de canal do cliente (whatsapp:+55...) → "Cliente"
    2. Literal "airton" (case-insensitive) → "Airton (IA)"  [novo UID desde 21/07/2026]
    3. Literal "system" (case-insensitive) → "Sistema (auto)"
    4. SID da própria conversa (autor = CH...) → "Sistema (auto)"  [saudação automática]
    5. Identity mapeado nos participantes → papel do mapa
    6. Endereço que parece número de telefone → "Cliente" (fallback)
    7. Qualquer outro → "Analista"
    """
    if autor in customer_addrs:
        return "Cliente"
    autor_low = autor.lower()
    if autor_low == "airton":
        return "Airton (IA)"
    if autor_low == "system":
        return "Sistema (auto)"
    if autor == conv_sid:
        return "Sistema (auto)"
    if autor in roles:
        return roles[autor]
    if _looks_like_customer_addr(autor):
        return "Cliente"
    return "Analista"


def build_transcript(conv: dict, messages: list, roles: dict) -> dict:
    """Monta um transcript legível + metadados da conversa.

    Papéis identificados:
    - Cliente      — endereço de WhatsApp/SMS do cliente externo
    - Airton (IA)  — mensagens com author='Airton' (novo UID desde 21/07/2026)
    - Analista     — atendente humano (identity no Flex)
    - Sistema (auto) — author='System' ou author=SID da conversa (mensagens automáticas:
                       saudação de roteamento, "cotação pronta", "pedido feito", etc.)
    """
    conv_sid = conv.get("sid") or ""
    customer_addrs = {addr for addr, papel in roles.items() if papel == "Cliente"}
    linhas = []
    for m in messages:
        autor = m.get("author") or ""
        papel = _classify_author(autor, conv_sid, customer_addrs, roles)
        corpo = (m.get("body") or "").strip()
        midia = m.get("media")
        if not corpo and midia:
            corpo = "[mídia/anexo]"
        ts = m.get("date_created") or ""
        linhas.append(f"[{ts}] {papel}: {corpo}")
    return {
        "conversation_sid": conv_sid,
        "friendly_name": conv.get("friendly_name") or "",
        "state": conv.get("state") or "",
        "date_created": conv.get("date_created"),
        "date_updated": conv.get("date_updated"),
        "attributes": conv.get("attributes"),
        "num_mensagens": len(messages),
        "transcript": "\n".join(linhas),
    }


# ---------------------------------------------------------------------------
# Auditoria via Claude (Anthropic API)
# ---------------------------------------------------------------------------

def build_audit_tool() -> dict:
    """Ferramenta que força o Claude a devolver JSON estruturado e limpo."""
    etapa_props = {}
    for e in ETAPA_ORDEM:
        etapa_props[e] = {
            "type": "object",
            "properties": {
                "nota": {"type": "integer",
                         "description": f"Nota de 0 a {ETAPA_MAX[e]} para a etapa {e} — {ETAPA_NOME[e]}."},
                "justificativa": {"type": "string",
                                  "description": "Por que essa nota, em 1-2 frases."},
                "trecho": {"type": "string",
                           "description": "Trecho literal da conversa que embasa a nota (ou '' se não aplicável)."},
            },
            "required": ["nota", "justificativa", "trecho"],
        }
    return {
        "name": "registrar_auditoria",
        "description": "Registra a avaliação da conversa etapa por etapa, seguindo a rubrica.",
        "input_schema": {
            "type": "object",
            "properties": {
                "etapas": {
                    "type": "object",
                    "properties": etapa_props,
                    "required": ETAPA_ORDEM,
                },
                "problemas": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Códigos de problema aplicáveis (ex.: P1, P4). Vazio se nenhum.",
                },
                "virtudes": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Códigos de virtude aplicáveis (ex.: V2, V3). Vazio se nenhum.",
                },
                "sugestoes_analista": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Sugestões objetivas de melhoria exclusivamente para o ANALISTA HUMANO. "
                                   "Não misture com melhorias do Airton. Vazio se não houver.",
                },
                "sugestoes_airton": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Sugestões objetivas de melhoria exclusivamente para o AIRTON (IA). "
                                   "Não misture com melhorias do analista. Vazio se não houver.",
                },
                "historico_task": {
                    "type": "string",
                    "description": "Resumo INTELIGENTE da conversa em 1-2 frases: o que o cliente queria e "
                                   "como o atendimento terminou. Sintetize com suas palavras — NÃO copie "
                                   "mensagens literais.",
                },
                "observacoes": {
                    "type": "string",
                    "description": "Observações gerais, categorias novas propostas, ressalvas de etapas em curso.",
                },
            },
            "required": ["etapas", "problemas", "virtudes",
                         "sugestoes_analista", "sugestoes_airton",
                         "historico_task", "observacoes"],
        },
    }


def classificacao_por_nota(total: int) -> str:
    if total >= 90:
        return "Excelente"
    if total >= 75:
        return "Bom"
    if total >= 60:
        return "Regular"
    if total >= 40:
        return "Abaixo do esperado"
    return "Crítico"


def load_manager_feedback(env: dict) -> str:
    """Lê feedbacks da gestora na planilha e retorna bloco de calibração para o system prompt.

    Lê a coluna 'feedback_gestora' e usa os registros com feedback preenchido como
    exemplos de calibração. Preserva o poder propositivo da auditoria: os feedbacks
    são tratados como contexto adicional, não como substituição da rubrica.
    """
    try:
        sheet_id_raw = env.get("GOOGLE_SHEET_ID", "").strip()
        if not sheet_id_raw:
            return ""
        import re as _re
        m = _re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", sheet_id_raw)
        sheet_id = m.group(1) if m else sheet_id_raw

        sa_file = find_service_account_file(env)
        token = google_access_token(sa_file)
        tab = env.get("AUDIT_SHEET_TAB") or "base_de_registros"
        tab = resolve_tab(sheet_id, tab, token)

        import urllib.parse as _up
        rng = _up.quote(f"{tab}!A1:Z2000", safe="")
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{rng}"
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if resp.status_code != 200:
            return ""
        values = resp.json().get("values", [])
        if not values:
            return ""
        header = values[0]
        if "feedback_gestora" not in header:
            return ""

        fb_idx = header.index("feedback_gestora")
        sid_idx = header.index("conversation_sid") if "conversation_sid" in header else None
        score_idx = header.index("score") if "score" in header else None
        hist_idx = header.index("historico_task") if "historico_task" in header else None
        resp_idx = header.index("responsavel_atendimento") if "responsavel_atendimento" in header else None

        examples = []
        for row in values[1:]:
            row_p = row + [""] * (len(header) - len(row))
            fb = row_p[fb_idx].strip() if fb_idx < len(row_p) else ""
            if not fb:
                continue
            sid = row_p[sid_idx].strip() if sid_idx is not None else ""
            score = row_p[score_idx].strip() if score_idx is not None else ""
            hist = (row_p[hist_idx].strip()[:200] if hist_idx is not None else "")
            resp_val = row_p[resp_idx].strip() if resp_idx is not None else ""
            examples.append(
                f"- Conversa {sid} (score={score}, analista={resp_val})\n"
                f"  Contexto: {hist}\n"
                f"  Feedback da gestora: {fb}"
            )

        if not examples:
            return ""

        bloco = (
            "\n\n---\n\n## Calibração por feedback da gestora de qualidade\n\n"
            "Os registros abaixo são feedbacks reais da gestora sobre auditorias anteriores. "
            "Use-os como referência de calibração para manter consistência de critério. "
            "Estes feedbacks **não substituem** a rubrica — use-os para ajustar o peso e o "
            "enquadramento de situações similares. Mantenha o rigor propositivo: continue "
            "identificando oportunidades de melhoria mesmo quando o analista compensou uma "
            "ineficiência do Airton. Ao distribuir responsabilidades, identifique sempre se "
            "a ação do analista foi uma correção intencional de um gap do Airton ou um "
            "comportamento independente.\n\n"
            + "\n".join(examples)
        )
        return bloco
    except Exception:
        return ""


def audit_transcript(env: dict, model: str, rubrica: str, transcript: dict) -> dict:
    """Chama o Claude e devolve a avaliação já com total e classificação calculados."""
    require(env, ["ANTHROPIC_API_KEY"], "auditar com o Claude")

    tool = build_audit_tool()
    feedback_block = load_manager_feedback(env)
    system = (
        rubrica
        + feedback_block
        + "\n\n---\n\nVocê receberá a transcrição de UMA conversa. Avalie cada etapa "
          "(E0 a E5) usando a ferramenta `registrar_auditoria`. Dê a nota de cada etapa "
          "dentro do teto indicado, sempre ancorando em um trecho real. NÃO invente fatos. "
          "Se uma etapa não chegou a acontecer na janela observada, pontue proporcionalmente "
          "e explique em `observacoes`. Ao distribuir oportunidades de melhoria, sempre "
          "separe responsabilidade do Airton (IA) e do analista humano. Quando o analista "
          "adotar um comportamento para corrigir uma ineficiência do Airton e melhorar a "
          "experiência do cliente, registre isso em `observacoes` e não penalize o analista "
          "por isso — penalize o Airton se o gap for dele."
    )
    user_content = (
        f"Conversa: {transcript['conversation_sid']} "
        f"(nome: {transcript['friendly_name'] or '—'}, estado: {transcript['state'] or '—'}, "
        f"{transcript['num_mensagens']} mensagens)\n\n"
        f"Transcrição:\n{transcript['transcript']}"
    )

    body = {
        "model": model,
        "max_tokens": 2000,
        "temperature": 0,
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": "registrar_auditoria"},
    }
    headers = {
        "x-api-key": env["ANTHROPIC_API_KEY"],
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    resp = requests.post(ANTHROPIC_URL, headers=headers, json=body, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"Anthropic API {resp.status_code}: {resp.text[:500]}")
    payload = resp.json()

    tool_input = None
    for block in payload.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "registrar_auditoria":
            tool_input = block.get("input")
            break
    if tool_input is None:
        raise RuntimeError(f"Resposta do Claude sem tool_use esperado: {json.dumps(payload)[:500]}")

    return finalize_evaluation(transcript, tool_input, model)


def finalize_evaluation(transcript: dict, tool_input: dict, model: str) -> dict:
    """Calcula total e classificação em Python (não confia na aritmética do modelo)."""
    etapas = {}
    total = 0
    for e in ETAPA_ORDEM:
        raw = (tool_input.get("etapas") or {}).get(e, {}) or {}
        nota = raw.get("nota", 0)
        try:
            nota = int(round(float(nota)))
        except (TypeError, ValueError):
            nota = 0
        nota = max(0, min(nota, ETAPA_MAX[e]))  # trava dentro do teto
        total += nota
        etapas[e] = {
            "nota": nota,
            "max": ETAPA_MAX[e],
            "nome": ETAPA_NOME[e],
            "justificativa": (raw.get("justificativa") or "").strip(),
            "trecho": (raw.get("trecho") or "").strip(),
        }
    return {
        "conversation_sid": transcript["conversation_sid"],
        "friendly_name": transcript["friendly_name"],
        "state": transcript["state"],
        "num_mensagens": transcript["num_mensagens"],
        "canal": transcript.get("canal") or "",
        "responsavel_atendimento": transcript.get("responsavel_atendimento") or "",
        "horario_conversa": transcript.get("date_created") or "",
        "nota_total": total,
        "classificacao": classificacao_por_nota(total),
        "etapas": etapas,
        "problemas": tool_input.get("problemas") or [],
        "virtudes": tool_input.get("virtudes") or [],
        "sugestoes": tool_input.get("sugestoes_analista") or [],
        "sugestoes_airton": tool_input.get("sugestoes_airton") or [],
        "historico_task": (tool_input.get("historico_task") or "").strip(),
        "observacoes": (tool_input.get("observacoes") or "").strip(),
        "modelo": model,
        # avaliado_em usa a data real da conversa (horario_conversa em BRT), não o momento do audit.
        # Isso evita que auditorias rodadas após meia-noite apareçam com data do dia seguinte.
        "avaliado_em": _conv_date_brt(transcript.get("date_created")),
    }


# ---------------------------------------------------------------------------
# Planilha Google
# ---------------------------------------------------------------------------

def google_access_token(sa_file: Path) -> str:
    """Assina um token de acesso a partir da conta de serviço (escopo Sheets)."""
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request as GoogleRequest
    except ImportError:
        print("[ERRO] Biblioteca google-auth ausente. Rode: "
              "pip install -r requirements.txt", file=sys.stderr)
        sys.exit(1)
    creds = service_account.Credentials.from_service_account_file(
        str(sa_file), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    creds.refresh(GoogleRequest())
    return creds.token


def _compute_gap_text(etapas_dict: dict) -> str:
    """Retorna texto legível dos pontos não conquistados por etapa.
    Ex: 'E1:−5 (Primeira Resposta) | E4:−20 (Follow-up Proativo)'
    """
    items = []
    for e, max_pts, nome in zip(ETAPA_ORDEM, ETAPA_MAX.values(), ETAPA_NOME.values()):
        nota = etapas_dict.get(e, {}).get("nota", 0) if isinstance(etapas_dict.get(e), dict) else 0
        diff = max_pts - nota
        if diff > 0:
            items.append(f"{e}:−{diff} ({nome})")
    return " | ".join(items)


def build_field_values(ev: dict) -> dict:
    """Monta {coluna_canônica: valor} para uma avaliação."""
    # evidencia_texto = trechos das etapas que perderam ponto (o que mais ensina);
    # se nada perdeu ponto, usa o primeiro trecho disponível.
    perdas = []
    for e in ETAPA_ORDEM:
        et = ev["etapas"][e]
        if et["trecho"] and et["nota"] < et["max"]:
            perdas.append(f"[{e}] {et['trecho']}")
    if not perdas:
        for e in ETAPA_ORDEM:
            if ev["etapas"][e]["trecho"]:
                perdas.append(f"[{e}] {ev['etapas'][e]['trecho']}")
                break
    evidencia = "  •  ".join(perdas)

    return {
        "data": ev["avaliado_em"],
        "horario_conversa": ev.get("horario_conversa") or "",
        "canal": ev.get("canal") or "",
        "responsavel_atendimento": ev.get("responsavel_atendimento") or "",
        "score": ev["nota_total"],
        "evidencia_texto": evidencia,
        "problemas_padronizados": ", ".join(ev["problemas"]),
        "virtudes_padronizadas": ", ".join(ev["virtudes"]),
        "sugestoes_melhoria": " | ".join(ev.get("sugestoes") or []),
        "sugestoes_analista": " | ".join(ev.get("sugestoes") or []),
        "sugestoes_airton": " | ".join(ev.get("sugestoes_airton") or []),
        "observacoes": ev["observacoes"],
        "classificacao": ev["classificacao"],
        "historico_task": ev.get("historico_task") or "",
        "conversation_sid": ev["conversation_sid"],
        "friendly_name": ev["friendly_name"],
        "state": ev["state"],
        "num_mensagens": ev["num_mensagens"],
        "nota_E0": ev["etapas"]["E0"]["nota"],
        "nota_E1": ev["etapas"]["E1"]["nota"],
        "nota_E2": ev["etapas"]["E2"]["nota"],
        "nota_E3": ev["etapas"]["E3"]["nota"],
        "nota_E4": ev["etapas"]["E4"]["nota"],
        "nota_E5": ev["etapas"]["E5"]["nota"],
        "gap_para_100": _compute_gap_text(ev["etapas"]),
        "modelo": ev["modelo"],
        "feedback_gestora": "",  # preenchido manualmente pela gestora no sheet
    }


def resolve_tab(sheet_id: str, tab: str, token: str) -> str:
    """Confirma que a aba existe; se não, usa a primeira aba da planilha."""
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers,
                        params={"fields": "sheets.properties.title"}, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Sheets API (metadados) {resp.status_code}: {resp.text[:500]}")
    titulos = [s["properties"]["title"] for s in resp.json().get("sheets", [])]
    if tab in titulos:
        return tab
    if titulos:
        print(f"  [aviso] aba '{tab}' não existe; usando a primeira aba: '{titulos[0]}'.",
              file=sys.stderr)
        return titulos[0]
    return tab


def read_header(sheet_id: str, tab: str, token: str) -> list:
    """Lê a primeira linha (cabeçalho) da aba. Lista vazia se não houver."""
    rng = f"{tab}!1:1"
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
           f"/values/{requests.utils.quote(rng, safe='')}")
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Sheets API (ler cabeçalho) {resp.status_code}: {resp.text[:500]}")
    values = resp.json().get("values", [])
    return values[0] if values else []


def write_header(sheet_id: str, tab: str, token: str, header: list):
    """Escreve/atualiza a linha de cabeçalho (a partir de A1)."""
    rng = f"{tab}!A1"
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
           f"/values/{requests.utils.quote(rng, safe='')}")
    headers = {"Authorization": f"Bearer {token}", "content-type": "application/json"}
    resp = requests.put(url, headers=headers, params={"valueInputOption": "RAW"},
                        json={"values": [header]}, timeout=30)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Sheets API (escrever cabeçalho) {resp.status_code}: {resp.text[:500]}")


def append_rows_to_sheet(sheet_id: str, tab: str, token: str, rows: list) -> dict:
    """Acrescenta linhas na planilha (values.append)."""
    rng = f"{tab}!A1"
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
           f"/values/{requests.utils.quote(rng, safe='')}:append")
    params = {"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"}
    headers = {"Authorization": f"Bearer {token}", "content-type": "application/json"}
    resp = requests.post(url, headers=headers, params=params, json={"values": rows}, timeout=30)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Sheets API {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def reconcile_header(existing: list) -> list:
    """Preserva o cabeçalho existente e acrescenta à direita as colunas canônicas ausentes."""
    if not existing:
        return list(CANONICAL_ORDER)
    cobertas = {canonical_for(cell) for cell in existing if canonical_for(cell)}
    faltantes = [c for c in CANONICAL_ORDER if c not in cobertas]
    return list(existing) + faltantes


def fetch_existing_sids(sheet_id: str, tab: str, token: str) -> set:
    """Retorna o conjunto de conversation_sids já gravados na planilha."""
    import urllib.parse as _up
    rng = _up.quote(f"{tab}!A1:Z2000", safe="")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{rng}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if resp.status_code != 200:
        return set()
    values = resp.json().get("values", [])
    if not values:
        return set()
    header = values[0]
    if "conversation_sid" not in header:
        return set()
    sid_idx = header.index("conversation_sid")
    return {row[sid_idx] for row in values[1:] if sid_idx < len(row) and row[sid_idx]}


def write_to_sheet(sheet_id: str, tab: str, token: str, evaluations: list) -> tuple:
    """Reconcilia o cabeçalho com a planilha existente e grava as linhas alinhadas a ele.
    Antes de gravar, filtra avaliações cujo conversation_sid já existe para evitar duplicatas.
    """
    tab = resolve_tab(sheet_id, tab, token)
    existing = read_header(sheet_id, tab, token)
    final_header = reconcile_header(existing)
    if final_header != existing:
        write_header(sheet_id, tab, token, final_header)

    # Deduplicação: não grava SIDs que já estão na planilha
    existing_sids = fetch_existing_sids(sheet_id, tab, token)
    novas_evals = []
    duplicatas = []
    for ev in evaluations:
        sid = ev.get("conversation_sid", "")
        if sid and sid in existing_sids:
            duplicatas.append(sid)
        else:
            novas_evals.append(ev)

    if duplicatas:
        print(f"  [dedup] {len(duplicatas)} SID(s) já existem na planilha — ignorados: "
              f"{', '.join(duplicatas[:3])}{'...' if len(duplicatas) > 3 else ''}")

    if not novas_evals:
        novas = [c for c in final_header if c not in (existing or [])]
        return tab, 0, novas

    rows = []
    for ev in novas_evals:
        vals = build_field_values(ev)
        row = []
        for cell in final_header:
            col = canonical_for(cell)
            row.append(vals.get(col, "") if col else "")
        rows.append(row)

    result = append_rows_to_sheet(sheet_id, tab, token, rows)
    updated = (result.get("updates") or {}).get("updatedRows", len(rows))
    novas = [c for c in final_header if c not in (existing or [])]
    return tab, updated, novas


# ---------------------------------------------------------------------------
# Impressão amigável
# ---------------------------------------------------------------------------

def print_evaluation(ev: dict):
    icon = {"Excelente": "🟢", "Bom": "🟢", "Regular": "🟡",
            "Abaixo do esperado": "🟠", "Crítico": "🔴"}.get(ev["classificacao"], "⚪")
    print(f"\n{icon} Conversa {ev['conversation_sid']} "
          f"({ev['friendly_name'] or '—'}) — {ev['num_mensagens']} mensagens")
    print(f"   Nota total: {ev['nota_total']}/100 → {ev['classificacao']}")
    if ev.get("historico_task"):
        print(f"   Histórico: {ev['historico_task']}")
    for e in ETAPA_ORDEM:
        et = ev["etapas"][e]
        print(f"   {e} {et['nome']}: {et['nota']}/{et['max']} — {et['justificativa']}")
    if ev["problemas"]:
        print(f"   Problemas: {', '.join(ev['problemas'])}")
    if ev["virtudes"]:
        print(f"   Virtudes:  {', '.join(ev['virtudes'])}")
    if ev.get("sugestoes"):
        print(f"   Sugestões: {' | '.join(ev['sugestoes'])}")
    if ev["observacoes"]:
        print(f"   Obs: {ev['observacoes']}")


# ---------------------------------------------------------------------------
# Resumo consolidado do dia (Mensagem 2) + publicação no Slack
# ---------------------------------------------------------------------------

def _nota10(total) -> float:
    """Converte a nota 0–100 para a escala /10 (uma casa). 74 -> 7.4"""
    try:
        return round(float(total) / 10.0, 1)
    except (TypeError, ValueError):
        return 0.0


def _pct_br(valor) -> str:
    """Formata número no padrão brasileiro (vírgula decimal). 7.4 -> '7,4'."""
    return f"{valor}".replace(".", ",")


def build_summary_tool() -> dict:
    """Ferramenta que força o Claude a devolver o resumo do dia em campos estruturados."""
    return {
        "name": "registrar_resumo",
        "description": (
            "Registra a síntese qualitativa do conjunto de auditorias do dia. "
            "NÃO invente fatos: baseie cada frase apenas nas auditorias fornecidas. "
            "Seja específico e acionável; evite generalidades vazias."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sentimento": {
                    "type": "string",
                    "description": (
                        "Uma frase sobre o sentimento geral do atendimento no dia "
                        "(ex: 'Positivo, com atenção à velocidade da primeira resposta')."
                    ),
                },
                "oportunidades_time": {
                    "type": "object",
                    "description": (
                        "Principal oportunidade de melhoria do TIME HUMANO em cada eixo. "
                        "Use string vazia se não houver nada relevante nas auditorias do dia."
                    ),
                    "properties": {
                        "velocidade": {"type": "string"},
                        "processo": {"type": "string"},
                        "eficiencia": {"type": "string"},
                        "cordialidade": {"type": "string"},
                    },
                    "required": ["velocidade", "processo", "eficiencia", "cordialidade"],
                },
                "oportunidades_airton": {
                    "type": "object",
                    "description": (
                        "Principal oportunidade de melhoria do AIRTON (assistente virtual IA) "
                        "em cada eixo. Use string vazia se não houver evidências nas auditorias. "
                        "Se o Airton não apareceu nas conversas auditadas, deixe tudo vazio."
                    ),
                    "properties": {
                        "roteamento": {"type": "string"},
                        "contexto_handoff": {"type": "string"},
                        "mensagens_automaticas": {"type": "string"},
                    },
                    "required": ["roteamento", "contexto_handoff", "mensagens_automaticas"],
                },
                "destaques": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "1 a 3 pontos positivos concretos observados (cite responsável ou "
                        "canal quando fizer sentido)."
                    ),
                },
                "proposta_semana": {
                    "type": "string",
                    "description": "UMA proposta acionável de melhoria para a semana.",
                },
            },
            "required": ["sentimento", "oportunidades_time", "oportunidades_airton",
                         "destaques", "proposta_semana"],
        },
    }


def _evaluations_digest(evaluations: list) -> str:
    """Compacta as auditorias em texto enxuto para alimentar a síntese."""
    linhas = []
    for ev in evaluations:
        etapas = " ".join(
            f"{e}={ev['etapas'][e]['nota']}/{ev['etapas'][e]['max']}" for e in ETAPA_ORDEM
        )
        linhas.append(
            f"- SID {ev['conversation_sid']} | resp: {ev.get('responsavel_atendimento') or '—'} | "
            f"canal: {ev.get('canal') or '—'} | nota: {ev['nota_total']}/100 ({ev['classificacao']}) | "
            f"{etapas}\n"
            f"    problemas: {'; '.join(ev.get('problemas') or []) or '—'}\n"
            f"    virtudes: {'; '.join(ev.get('virtudes') or []) or '—'}\n"
            f"    sugestoes_analista: {' | '.join(ev.get('sugestoes') or []) or '—'}\n"
            f"    sugestoes_airton: {' | '.join(ev.get('sugestoes_airton') or []) or '—'}\n"
            f"    obs: {ev.get('observacoes') or '—'}"
        )
    return "\n".join(linhas)


def generate_summary(env: dict, model: str, evaluations: list) -> dict:
    """Uma chamada ao Claude para sintetizar o dia (Mensagem 2)."""
    require(env, ["ANTHROPIC_API_KEY"], "gerar o resumo do dia")
    tool = build_summary_tool()
    system = (
        "Você é o auditor-líder do atendimento da Mecanizou. Recebe as auditorias "
        "individuais do dia e produz uma síntese executiva curta, específica e acionável, "
        "usando a ferramenta `registrar_resumo`. Fale a partir das evidências fornecidas; "
        "não invente números nem fatos que não estejam nas auditorias."
    )
    user_content = (
        f"Auditorias do dia ({len(evaluations)} conversas):\n\n"
        f"{_evaluations_digest(evaluations)}\n\n"
        "Sintetize as oportunidades do TIME HUMANO nos eixos Velocidade, Processo, Eficiência e "
        "Cordialidade, e separadamente as oportunidades do AIRTON (IA) nos eixos Roteamento, "
        "Contexto de Handoff e Mensagens Automáticas. Destaque o que foi bem e proponha UMA "
        "ação para a semana."
    )
    body = {
        "model": model,
        "max_tokens": 1500,
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": "registrar_resumo"},
    }
    headers = {
        "x-api-key": env["ANTHROPIC_API_KEY"],
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    resp = requests.post(ANTHROPIC_URL, headers=headers, json=body, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"Anthropic API {resp.status_code}: {resp.text[:500]}")
    payload = resp.json()
    for block in payload.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "registrar_resumo":
            return block.get("input") or {}
    raise RuntimeError(f"Resposta do Claude sem tool_use esperado: {json.dumps(payload)[:500]}")


def render_summary_slack(evaluations: list, summary: dict) -> str:
    """Formata a Mensagem 2 (resumo do dia) em mrkdwn do Slack, de forma determinística."""
    n = len(evaluations)
    media100 = sum(ev["nota_total"] for ev in evaluations) / n if n else 0.0
    media10 = _nota10(media100)

    contagem = {"🟢": 0, "🟡": 0, "🟠": 0, "🔴": 0}
    for ev in evaluations:
        icon = {"Excelente": "🟢", "Bom": "🟢", "Regular": "🟡",
                "Abaixo do esperado": "🟠", "Crítico": "🔴"}.get(ev["classificacao"], "⚪")
        contagem[icon] = contagem.get(icon, 0) + 1
    dist = " · ".join(f"{k} {v}" for k, v in contagem.items() if v)

    ts = datetime.now(BRT).strftime("%d/%m")
    op_time = summary.get("oportunidades_time") or {}
    op_airton = summary.get("oportunidades_airton") or {}
    eixos_time = [
        ("Velocidade", op_time.get("velocidade")),
        ("Processo", op_time.get("processo")),
        ("Eficiência", op_time.get("eficiencia")),
        ("Cordialidade", op_time.get("cordialidade")),
    ]
    eixos_airton = [
        ("Roteamento", op_airton.get("roteamento")),
        ("Contexto de handoff", op_airton.get("contexto_handoff")),
        ("Mensagens automáticas", op_airton.get("mensagens_automaticas")),
    ]

    lines = [
        f"*Auditoria do dia — {ts} (amostra: {n} conversas)*",
        "",
        f"*Score médio:* {_pct_br(media10)}/10  ·  {dist}",
        "",
        f"*Sentimento:* {summary.get('sentimento') or '—'}",
        "",
        "*Oportunidades do time*",
    ]
    time_items = [(n2, t) for n2, t in eixos_time if t and t.strip()]
    for nome, txt in time_items:
        lines.append(f"• {nome}: {txt.strip()}")
    if not time_items:
        lines.append("_Nenhuma oportunidade identificada para o time._")

    airton_items = [(n2, t) for n2, t in eixos_airton if t and t.strip()]
    lines.append("")
    lines.append("*Oportunidades do Airton (IA)*")
    for nome, txt in airton_items:
        lines.append(f"• {nome}: {txt.strip()}")
    if not airton_items:
        lines.append("_Nenhuma oportunidade identificada para o Airton._")

    destaques = [d for d in (summary.get("destaques") or []) if d and d.strip()]
    if destaques:
        lines.append("")
        lines.append("*Destaques*")
        for d in destaques:
            lines.append(f"• {d.strip()}")

    proposta = (summary.get("proposta_semana") or "").strip()
    if proposta:
        lines.append("")
        lines.append("*Proposta da semana*")
        lines.append(proposta)

    return "\n".join(lines)


def post_to_slack(token: str, channel: str, text: str) -> dict:
    """Publica uma mensagem no Slack via chat.postMessage."""
    body = json.dumps({"channel": channel, "text": text, "mrkdwn": True}).encode()
    req = requests.post(
        "https://slack.com/api/chat.postMessage",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        timeout=30,
    )
    resp = req.json()
    if not resp.get("ok"):
        raise RuntimeError(f"Slack respondeu erro: {resp}")
    return resp


# ---------------------------------------------------------------------------
# Fontes de transcript
# ---------------------------------------------------------------------------

def transcripts_from_twilio(env: dict, args) -> list:
    require(env, ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"], "buscar conversas no Twilio")
    auth = (env["TWILIO_ACCOUNT_SID"], env["TWILIO_AUTH_TOKEN"])

    if args.conversation_sid:
        sids = [s.strip() for s in args.conversation_sid.split(",") if s.strip()]
        convs = [get_conversation(auth, s) for s in sids]
    else:
        seed = args.seed if args.seed is not None else datetime.now(timezone.utc).strftime("%Y-%m-%d")
        convs, meta = select_conversations_for_audit(
            auth, args.sample, args.state,
            long_min=args.long_min, long_share=args.long_share, seed=seed)
        print(f"[amostra] {meta['selecionadas']} de {meta['pool']} conversas '{args.state}' "
              f"({meta['longas_no_pool']} longas >{args.long_min:.0f}min no pool). "
              f"Analistas cobertos: {meta['analistas_identificados']} | "
              f"Airton-only: {meta['airton_only_selecionadas']} "
              f"(pool: {meta['airton_only_no_pool']}).",
              file=sys.stderr)

    if not convs:
        print("Nenhuma conversa encontrada com esses critérios.", file=sys.stderr)
        return []

    out = []
    for conv in convs:
        sid = conv.get("sid")
        participants = fetch_participants(auth, sid)
        roles = roles_from_participants(participants)
        messages = fetch_messages(auth, sid)
        t = build_transcript(conv, messages, roles)
        t["canal"] = canal_from_participants(participants)
        t["responsavel_atendimento"] = atendente_from_participants(participants)
        out.append(t)
    return out


def transcript_from_fixture(path: Path) -> list:
    """Lê um transcript de arquivo local para validar a lógica sem Twilio.

    Aceita JSON no formato {conversation_sid, friendly_name, state, num_mensagens,
    transcript} OU um .txt/.md que vira o próprio transcript.
    """
    if not path.is_absolute():
        path = BASE_DIR / path
    if not path.exists():
        print(f"[ERRO] Fixture não encontrada: {path}", file=sys.stderr)
        sys.exit(1)
    raw = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        d = json.loads(raw)
        d.setdefault("conversation_sid", path.stem)
        d.setdefault("friendly_name", "")
        d.setdefault("state", "")
        d.setdefault("num_mensagens", d.get("transcript", "").count("\n") + 1)
        return [d]
    return [{
        "conversation_sid": path.stem, "friendly_name": "", "state": "",
        "num_mensagens": raw.count("\n") + 1, "transcript": raw,
    }]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Agente de Auditoria de Atendimento — Mecanizou")
    parser.add_argument("--sample", type=int, default=10,
                        help="Quantas conversas auditar por rodada (padrão: 10/dia)")
    parser.add_argument("--conversation-sid", type=str, default=None,
                        help="SID(s) de conversa específicos (separados por vírgula)")
    parser.add_argument("--state", type=str, default="closed",
                        choices=["active", "inactive", "closed", "all"],
                        help="Filtra conversas por estado (padrão: closed = só encerradas)")
    parser.add_argument("--long-min", type=float, default=60.0,
                        help="Minutos para considerar uma conversa 'longa' (padrão: 60)")
    parser.add_argument("--long-share", type=float, default=0.7,
                        help="Fração da amostra que vem de conversas longas (padrão: 0.7)")
    parser.add_argument("--seed", type=str, default=None,
                        help="Semente da randomização (padrão: data do dia, p/ reprodutibilidade)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Audita e imprime, mas NÃO grava na planilha")
    parser.add_argument("--raw", action="store_true",
                        help="Só baixa e mostra o JSON bruto do Twilio (sem IA, sem planilha)")
    parser.add_argument("--fixture", type=str, default=None,
                        help="Audita um transcript de arquivo local (pula o Twilio)")
    parser.add_argument("--model", type=str, default=None,
                        help="Modelo Claude (padrão: env AUDIT_MODEL ou claude-sonnet-4-6)")
    parser.add_argument("--save-local", action="store_true",
                        help="Salva também um JSON de cada avaliação em estudos/auditorias/")
    parser.add_argument("--post-slack", action="store_true",
                        help="Gera o resumo do dia (Mensagem 2) e publica no Slack")
    parser.add_argument("--summary-only", action="store_true",
                        help="Com --post-slack, imprime o resumo sem publicar (para conferência)")
    args = parser.parse_args()

    env = load_env()
    model = args.model or env.get("AUDIT_MODEL") or DEFAULT_MODEL
    tab = env.get("AUDIT_SHEET_TAB") or DEFAULT_SHEET_TAB

    # Modo --raw: apenas inspecionar o formato do Twilio
    if args.raw:
        require(env, ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"], "inspecionar o Twilio")
        auth = (env["TWILIO_ACCOUNT_SID"], env["TWILIO_AUTH_TOKEN"])
        if args.conversation_sid:
            convs = [get_conversation(auth, args.conversation_sid.split(",")[0].strip())]
        else:
            convs = list_conversations(auth, args.sample, args.state)
        for conv in convs:
            sid = conv.get("sid")
            print(f"\n===== CONVERSA {sid} =====")
            print(json.dumps(conv, ensure_ascii=False, indent=2))
            print("----- PARTICIPANTES -----")
            participants = fetch_participants(auth, sid)
            print(json.dumps(participants, ensure_ascii=False, indent=2))
            print(f"  papeis: {roles_from_participants(participants)}")
            print(f"  canal: {canal_from_participants(participants)}  "
                  f"responsavel: {atendente_from_participants(participants)}")
            print("----- MENSAGENS (bruto) -----")
            print(json.dumps(fetch_messages(auth, sid)[:20], ensure_ascii=False, indent=2))
        return

    # Fonte dos transcripts
    if args.fixture:
        transcripts = transcript_from_fixture(Path(args.fixture))
    else:
        transcripts = transcripts_from_twilio(env, args)

    if not transcripts:
        slack_token = env.get("SLACK_BOT_TOKEN")
        if slack_token and not args.dry_run:
            from datetime import datetime, timezone, timedelta
            BRT = timezone(timedelta(hours=-3))
            ts_now = datetime.now(BRT).strftime("%d/%m/%Y %H:%M")
            post_to_slack(
                slack_token,
                "D06RTBVPUUT",
                f":warning: *Auditoria sem conversas* ({ts_now})\n"
                "Nenhuma conversa foi selecionada para auditoria nesta rodada.\n"
                "Verifique os logs da Action ou se há conversas concluídas no Twilio.",
            )
        return

    rubrica = RUBRICA_PATH.read_text(encoding="utf-8")

    evaluations = []
    for t in transcripts:
        try:
            ev = audit_transcript(env, model, rubrica, t)
        except Exception as e:
            print(f"[ERRO] Falha ao auditar {t.get('conversation_sid')}: {e}", file=sys.stderr)
            continue
        evaluations.append(ev)
        print_evaluation(ev)

    if not evaluations:
        print("Nenhuma avaliação concluída.", file=sys.stderr)
        slack_token = env.get("SLACK_BOT_TOKEN")
        if slack_token and not args.dry_run:
            from datetime import datetime, timezone, timedelta
            BRT = timezone(timedelta(hours=-3))
            ts_now = datetime.now(BRT).strftime("%d/%m/%Y %H:%M")
            try:
                post_to_slack(
                    slack_token,
                    "D06RTBVPUUT",
                    f":red_circle: *Auditoria: 0 avaliações concluídas* ({ts_now})\n"
                    "As conversas foram selecionadas mas todas falharam durante a avaliação.\n"
                    "Verifique os logs da Action.",
                )
            except Exception as e:
                print(f"[ERRO] DM Slack: {e}", file=sys.stderr)
        sys.exit(1)

    if args.save_local:
        out_dir = BASE_DIR / "estudos" / "auditorias"
        out_dir.mkdir(parents=True, exist_ok=True)
        for ev in evaluations:
            stamp = datetime.now(BRT).strftime("%Y-%m-%d_%H%M%S")
            fp = out_dir / f"{ev['conversation_sid']}_{stamp}.json"
            fp.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[ok] {len(evaluations)} avaliação(ões) salva(s) em {out_dir}")

    # Resumo consolidado do dia (Mensagem 2) + publicação no Slack.
    # Roda independentemente da planilha; com --summary-only apenas imprime.
    if args.post_slack:
        try:
            summary = generate_summary(env, model, evaluations)
            slack_text = render_summary_slack(evaluations, summary)
        except Exception as e:
            print(f"[ERRO] Falha ao gerar o resumo do dia: {e}", file=sys.stderr)
            slack_text = None
        if slack_text:
            print("\n===== RESUMO DO DIA (Mensagem 2) =====")
            print(slack_text)
            if args.summary_only:
                print("\n[summary-only] Resumo NÃO publicado no Slack.")
            else:
                require(env, ["SLACK_BOT_TOKEN", "TWILIO_SLACK_CHANNEL_ID"],
                        "publicar o resumo no Slack")
                try:
                    r = post_to_slack(env["SLACK_BOT_TOKEN"],
                                      env["TWILIO_SLACK_CHANNEL_ID"], slack_text)
                    print(f"\n[ok] Resumo publicado no Slack (ts={r.get('ts')}).")
                except Exception as e:
                    print(f"[ERRO] Não consegui publicar no Slack: {e}", file=sys.stderr)

    # Gravação na planilha (a menos que dry-run)
    if args.dry_run:
        print("\n[dry-run] Nada gravado na planilha. "
              "Remova --dry-run quando as notas fizerem sentido.")
        return

    require(env, ["GOOGLE_SHEET_ID"], "gravar na planilha Google")
    sheet_id = normalize_sheet_id(env["GOOGLE_SHEET_ID"])
    sa_file = find_service_account_file(env)
    token = google_access_token(sa_file)
    tab_usada, updated, novas = write_to_sheet(sheet_id, tab, token, evaluations)
    print(f"\n[ok] {updated} linha(s) gravada(s) na planilha (aba '{tab_usada}').")
    if novas:
        print(f"     Colunas novas acrescentadas à direita: {', '.join(novas)}")


if __name__ == "__main__":
    main()
