"""
Painel de processamento de auditorias ANP.

Suporta upload em qualquer ordem, um arquivo por vez ou em lote: se uma
resposta/parecer chegar antes da auditoria correspondente existir, o
resultado já extraído fica numa fila de pendências e é resolvido
automaticamente assim que a auditoria (ou o item específico) aparecer —
sem gastar tokens de novo.

Rodar localmente:
    streamlit run app.py

Configuração (via .streamlit/secrets.toml ou variáveis de ambiente):
    ANTHROPIC_API_KEY
    SUPABASE_URL
    SUPABASE_KEY
"""

import hashlib
import io
import json
import os
import re

import pdfplumber
import streamlit as st
from anthropic import Anthropic

import db

# Número de processo da ANP segue um formato fixo (ex: 48610.221768/2025-20).
# Extrair por regex direto do texto é mais confiável do que depender da IA
# "adivinhar" qual número do documento é o processo — evita que documentos do
# mesmo processo fiquem com o campo escrito de formas ligeiramente diferentes
# e, por isso, não se vinculem entre si.
#
# A classe [-‐‑‒–—−] cobre o hífen comum e os travessões "parecidos" que PDFs
# às vezes usam (en-dash, non-breaking hyphen etc.) — sem isso, dois
# documentos do mesmo processo podem extrair o número com traços diferentes
# e nunca dar match.
REGEX_PROCESSO_ANP = re.compile(r"\d{4,5}\.\d{6}/\d{4}[-‐‑‒–—−]\d{2}")


def extract_processo_regex(texto):
    # Só aceita um número que apareça perto de uma âncora que indica que é o
    # processo DESTE documento — rodapé padrão do SEI ("Processo nº ... SEI
    # nº ...") ou "Processo Administrativo ...". Sem isso, anexos técnicos que
    # citam processos de OUTRAS auditorias como precedente no meio do texto
    # (ex: "conforme processo 48610.222892/2024-21...") fariam essa regex
    # roubar um número errado e sobrescrever o que a IA extraiu corretamente.
    for busca in (texto, re.sub(r"\s+", " ", texto)):
        for m in REGEX_PROCESSO_ANP.finditer(busca):
            antes = busca[max(0, m.start() - 30):m.start()].lower()
            depois = busca[m.end():m.end() + 15].lower()
            if "administrativo" in antes or "sei" in depois:
                return normalize_processo(m.group(0))
    return None


def normalize_processo(numero):
    if not numero:
        return numero
    # unifica qualquer variação de travessão para o hífen comum
    for ch in "‐‑‒–—−":
        numero = numero.replace(ch, "-")
    return numero.strip()

st.set_page_config(page_title="Auditorias ANP", layout="wide")

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
def get_secret(name, default=None):
    if name in st.secrets:
        return st.secrets[name]
    return os.environ.get(name, default)

SUPABASE_URL = get_secret("SUPABASE_URL")
SUPABASE_KEY = get_secret("SUPABASE_KEY")

# "claude" ou "ollama" — é essa a única linha que você precisa trocar para
# alternar entre a API da Anthropic e um modelo local via Ollama.
LLM_PROVIDER = get_secret("LLM_PROVIDER", "claude").strip().lower()

ANTHROPIC_API_KEY = get_secret("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = get_secret("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

OLLAMA_HOST = get_secret("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = get_secret("OLLAMA_MODEL", "qwen2.5:7b-instruct")
# Contexto menor por padrão para modelo local: em 16GB de memória unificada,
# um prompt gigante empurra o KV cache além do que sobra depois do peso do
# modelo + sistema operacional. Ajuste para cima se sua máquina aguentar.
OLLAMA_MAX_CHARS = int(get_secret("OLLAMA_MAX_CHARS", "40000"))
OLLAMA_NUM_CTX = int(get_secret("OLLAMA_NUM_CTX", "16384"))

CLAUDE_MAX_CHARS = int(get_secret("CLAUDE_MAX_CHARS", "80000"))

if not (SUPABASE_URL and SUPABASE_KEY):
    st.error("Faltam credenciais do Supabase (SUPABASE_URL, SUPABASE_KEY) em .streamlit/secrets.toml.")
    st.stop()

if LLM_PROVIDER == "claude" and not ANTHROPIC_API_KEY:
    st.error("LLM_PROVIDER está como 'claude', mas falta ANTHROPIC_API_KEY em .streamlit/secrets.toml.")
    st.stop()

sb = db.get_client(SUPABASE_URL, SUPABASE_KEY)

_claude_client = None
if LLM_PROVIDER == "claude":
    from anthropic import Anthropic
    _claude_client = Anthropic(api_key=ANTHROPIC_API_KEY)

CONFIDENCE_THRESHOLD = 0.7
MAX_CHARS = OLLAMA_MAX_CHARS if LLM_PROVIDER == "ollama" else CLAUDE_MAX_CHARS
CURRENT_MODEL_LABEL = f"ollama:{OLLAMA_MODEL}" if LLM_PROVIDER == "ollama" else f"claude:{ANTHROPIC_MODEL}"

PROMPT = """Você processa documentos de auditorias da ANP (regulação de petróleo e gás no Brasil).
Analise o texto abaixo e responda SOMENTE com JSON válido, sem markdown, sem texto fora do JSON.

Formato exato:
{{
  "tipo_documento": "auditoria_oficial|resposta_operadora|parecer_anp|evidencia_anexa|indeterminado",
  "confianca": 0.0,
  "numero_processo_anp": "string ou null",
  "numero_relatorio": "string ou null",
  "auditoria": null,
  "nao_conformidades": [],
  "respostas": []
}}

Preencha "auditoria" (objeto com codigo_auditoria_anp, ordem_servico, operadora, cnpj_operadora,
unidade_instalacao, tipo_auditoria, sumario_auditoria, acao_demandada, data_auditoria_inicio,
data_auditoria_fim, data_emissao_relatorio, auditor_responsavel, status_auditoria, resultado_final)
e "nao_conformidades" (array de {{numero_item, descricao, categoria_nao_conformidade,
norma_referencia, classificacao_gravidade, acao_recomendada, prazo_correcao}}) SOMENTE se
tipo_documento for auditoria_oficial. O anexo técnico de uma auditoria também conta como
auditoria_oficial.

Detalhe de cada campo novo de "auditoria":
- codigo_auditoria_anp: identificador interno da própria ANP para a auditoria, geralmente no
  formato "NNN_SSO_AAAA" (ex: "027_SSO_2025"), citado como "Auditoria 027_SSO_2025" no texto.
  Diferente do número do processo administrativo e do número do relatório/DF.
- ordem_servico: número da Ordem de Serviço, geralmente no cabeçalho do documento (ex: "OS SSO_002725").
- sumario_auditoria: resumo em até 40 palavras do que a auditoria constatou/tratou.
- acao_demandada: a medida cautelar ou ação exigida pela ANP como resultado da auditoria (ex:
  "Interdição da operação com hidrocarbonetos até saneamento das notificações"), em até 30 palavras.
- resultado_final: a conclusão/status final da auditoria SE o documento deixar isso claro (ex:
  "interditada", "liberada para operação"); use null se não houver essa informação.

Detalhe de cada campo novo de "nao_conformidades":
- categoria_nao_conformidade: área temática do item (ex: "Gestão de risco", "Sistema de drenagem",
  "Integridade estrutural", "Comunicação", "Atmosfera explosiva"), em 2-4 palavras.
- acao_recomendada: a ação que a ANP exige da operadora para sanar especificamente este item,
  em até 20 palavras (geralmente o próprio texto da notificação, ex: "Refazer o estudo de
  dispersão de gases considerando condição de calmaria").

Preencha "respostas" (array de {{numero_item, resultado_final, decisao_anp, resumo}}) SOMENTE se

ATENÇÃO — numeração dos itens em Documentos de Fiscalização da ANP: é comum o documento ter DUAS
listas sobre os mesmos assuntos, com numerações diferentes:
  (a) uma seção descritiva/técnica (ex: "3-AUTO DE INTERDIÇÃO", itens "3.1", "3.2", "3.4"...),
      com o detalhamento de cada problema encontrado;
  (b) uma seção de NOTIFICAÇÃO formal (ex: "5-NOTIFICAÇÃO", itens "5.1", "5.4", "5.6"...), que
      instrui a operadora a comprovar/corrigir/apresentar evidências sobre cada assunto.
As respostas da operadora e os pareceres da ANP SEMPRE referenciam a numeração da seção (b), a de
NOTIFICAÇÃO. Portanto, o campo "numero_item" de cada não conformidade DEVE usar o número da seção
de notificação formal (ex: "5.4"), mesmo que você use o texto da seção descritiva (a) para preencher
"descricao" com mais detalhe. Nunca use a numeração da seção descritiva (ex: "3.1") como numero_item
se existir uma seção de notificação formal correspondente no mesmo documento.

Preencha "respostas" (array de {{numero_item, resultado_final, decisao_anp, resumo}}) SOMENTE se
tipo_documento for resposta_operadora ou parecer_anp. numero_item deve corresponder exatamente ao
número usado pelo próprio documento ao citar a não conformidade/condicionante (ex: "5.4"). resumo
em até 25 palavras.

Responda APENAS o JSON, começando com {{ e terminando com }}, sem nenhum texto antes ou depois.

TEXTO DO DOCUMENTO (pode estar truncado):
---
{texto}
---
"""


# ---------------------------------------------------------------------------
# Extração de texto
# ---------------------------------------------------------------------------
def extract_text(file_bytes, max_chars=None):
    max_chars = max_chars or MAX_CHARS
    parts = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n".join(parts)[:max_chars]


# ---------------------------------------------------------------------------
# Camada de LLM — troque LLM_PROVIDER no secrets para alternar entre os dois
# ---------------------------------------------------------------------------
def _clean_json_text(raw):
    raw = raw.strip().strip("`")
    if raw.lower().startswith("json"):
        raw = raw[4:].strip()
    # alguns modelos locais "conversam" antes/depois do JSON mesmo quando
    # instruídos a não fazer isso — corta para o primeiro { e o último }
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1:
        raw = raw[start:end + 1]
    return raw


def _call_claude(prompt):
    msg = _claude_client.messages.create(
        model=ANTHROPIC_MODEL,
        # Documentos de auditoria reais costumam ter 10-40+ não conformidades,
        # cada uma com vários campos — 2000 tokens cortava o JSON no meio e
        # quebrava com JSONDecodeError antes mesmo de salvar qualquer coisa.
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if b.type == "text")


def _call_ollama(prompt):
    import requests
    resp = requests.post(
        f"{OLLAMA_HOST}/api/generate",
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0, "num_ctx": OLLAMA_NUM_CTX},
        },
        timeout=300,
    )
    resp.raise_for_status()
    return resp.json()["response"]


def classify_and_extract(texto):
    prompt = PROMPT.format(texto=texto)
    raw = _call_ollama(prompt) if LLM_PROVIDER == "ollama" else _call_claude(prompt)
    return json.loads(_clean_json_text(raw))


# ---------------------------------------------------------------------------
# Lógica de vínculo — reutilizada tanto no upload quanto na fila de retentativa
# ---------------------------------------------------------------------------
def apply_auditoria(result, filename):
    """auditoria_oficial sempre "resolve" na hora: cria ou atualiza."""
    aud = {k: v for k, v in {
        "numero_processo_anp": result.get("numero_processo_anp"),
        "numero_relatorio": result.get("numero_relatorio"),
        **(result.get("auditoria") or {}),
    }.items() if v is not None}
    id_auditoria = db.upsert_auditoria(sb, aud, filename)
    for nc in result.get("nao_conformidades", []):
        db.upsert_nc(sb, id_auditoria, nc, filename)
    return id_auditoria, len(result.get("nao_conformidades", []))


def try_apply_respostas(numero_processo_anp, numero_relatorio, respostas, tipo, filename):
    """Tenta vincular cada resposta a uma não conformidade existente.

    Retorna (status, itens_nao_resolvidos, mensagem).
    status: 'concluido' | 'parcial' | 'pendente_vinculo'
    """
    aud = db.find_auditoria(sb, numero_processo_anp, numero_relatorio)
    if not aud:
        return "pendente_vinculo", respostas, "auditoria ainda não cadastrada"

    resolvidos, pendentes = 0, []
    for r in respostas:
        nc = db.find_nc(sb, aud["id_auditoria"], r.get("numero_item"))
        if not nc:
            pendentes.append(r)
            continue
        db.insert_resposta(sb, nc["id_nao_conformidade"], aud["id_auditoria"], tipo, r, filename)
        resolvidos += 1

    if not pendentes:
        return "concluido", [], f"{resolvidos} resposta(s) vinculada(s) à auditoria {aud['id_auditoria']}"
    if resolvidos == 0:
        return "pendente_vinculo", pendentes, "item(ns) de não conformidade ainda não cadastrado(s)"
    return "parcial", pendentes, f"{resolvidos} vinculada(s), {len(pendentes)} aguardando item correspondente"


def retry_pending_queue():
    """Roda depois de toda auditoria nova: tenta resolver a fila de pendências."""
    resolvidos = []
    for item in db.fetch_pendentes(sb):
        payload = item["payload_json"] or {}
        status, pendentes, msg = try_apply_respostas(
            payload.get("numero_processo_anp"),
            payload.get("numero_relatorio"),
            payload.get("respostas", []),
            payload.get("tipo_documento"),
            item["nome_arquivo"],
        )
        if status != item["status"] or len(pendentes) != len(payload.get("respostas", [])):
            novo_payload = {**payload, "respostas": pendentes} if pendentes else None
            db.upsert_arquivo_status(
                sb, item["hash_arquivo"], item["nome_arquivo"], item["tipo_documento"],
                item["confianca"], status, msg, novo_payload,
            )
            if status == "concluido":
                resolvidos.append(item["nome_arquivo"])
    return resolvidos


# ---------------------------------------------------------------------------
# Processamento de um upload
# ---------------------------------------------------------------------------
def process_uploaded_file(uploaded_file, force=False):
    file_bytes = uploaded_file.getvalue()
    file_hash = hashlib.sha256(file_bytes).hexdigest()

    existing = db.already_processed(sb, file_hash)
    if existing and existing["status"] in ("concluido", "revisao_manual", "erro") and not force:
        return "duplicado", f"já processado antes ({existing['status']}) — marque 'forçar reprocessamento' para refazer"

    texto = extract_text(file_bytes)
    if not texto.strip():
        db.upsert_arquivo_status(sb, file_hash, uploaded_file.name, None, 0.0, "erro", "sem texto extraível (pode precisar de OCR)")
        return "erro", "sem texto extraível (pode precisar de OCR)"

    result = classify_and_extract(texto)
    tipo = result["tipo_documento"]

    # Override determinístico: se o texto contém um número de processo ANP no
    # formato padrão, usa ele em vez do que a IA extraiu — garante que
    # documentos do mesmo processo sempre casem entre si.
    numero_regex = extract_processo_regex(texto)
    if numero_regex:
        result["numero_processo_anp"] = numero_regex

    if result["confianca"] < CONFIDENCE_THRESHOLD or tipo in ("indeterminado", "evidencia_anexa"):
        db.upsert_arquivo_status(sb, file_hash, uploaded_file.name, tipo, result["confianca"], "revisao_manual", "confiança baixa ou tipo não processável automaticamente")
        return "revisao", f"confiança {result['confianca']:.2f} — tipo {tipo}"

    processo_label = f" [processo {result.get('numero_processo_anp') or '—'}]"

    if tipo == "auditoria_oficial":
        id_auditoria, n = apply_auditoria(result, uploaded_file.name)
        db.upsert_arquivo_status(sb, file_hash, uploaded_file.name, tipo, result["confianca"], "concluido", f"auditoria {id_auditoria}")
        resolvidos = retry_pending_queue()
        detalhe = f"auditoria {id_auditoria} atualizada com {n} não conformidade(s){processo_label}"
        if resolvidos:
            detalhe += f" — também resolveu da fila: {', '.join(resolvidos)}"
        return "concluido", detalhe

    # resposta_operadora ou parecer_anp
    status, pendentes, msg = try_apply_respostas(
        result.get("numero_processo_anp"), result.get("numero_relatorio"),
        result.get("respostas", []), tipo, uploaded_file.name,
    )
    msg += processo_label
    payload = None
    if pendentes:
        payload = {
            "tipo_documento": tipo,
            "numero_processo_anp": result.get("numero_processo_anp"),
            "numero_relatorio": result.get("numero_relatorio"),
            "respostas": pendentes,
        }
    db.upsert_arquivo_status(sb, file_hash, uploaded_file.name, tipo, result["confianca"], status, msg, payload)
    status_label = {"concluido": "concluido", "parcial": "revisao", "pendente_vinculo": "revisao"}[status]
    return status_label, msg


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------
st.title("Painel de auditorias ANP")
st.caption(f"Classificação e extração via {CURRENT_MODEL_LABEL} · banco Supabase · upload em qualquer ordem")

if LLM_PROVIDER == "ollama":
    try:
        import requests
        tags = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=3).json().get("models", [])
        nomes = [m["name"] for m in tags]
        if not any(OLLAMA_MODEL.split(":")[0] in n for n in nomes):
            st.warning(
                f"Ollama está rodando em {OLLAMA_HOST}, mas o modelo '{OLLAMA_MODEL}' não "
                f"aparece na lista de modelos baixados ({nomes or 'nenhum'}). "
                f"Rode: `ollama pull {OLLAMA_MODEL}`"
            )
    except Exception:
        st.error(
            f"Não consegui conectar ao Ollama em {OLLAMA_HOST}. Confirme que ele está rodando "
            f"(`ollama serve` ou abra o app Ollama) e que OLLAMA_HOST está correto."
        )
        st.stop()

uploaded_files = st.file_uploader(
    "Envie PDFs de auditoria, resposta ou parecer — em qualquer ordem, um de cada vez ou em lote",
    type=["pdf"],
    accept_multiple_files=True,
)
force_reprocess = st.checkbox(
    "Forçar reprocessamento (mesmo se este arquivo já foi processado antes)",
    help="Use isso para refazer um arquivo que ficou com vínculo errado por causa de "
         "uma correção no sistema — ele vai chamar a IA de novo e sobrescrever o resultado salvo.",
)

if uploaded_files and st.button(f"Processar {len(uploaded_files)} arquivo(s)", type="primary"):
    progress = st.progress(0.0)
    for i, f in enumerate(uploaded_files):
        with st.spinner(f"Processando {f.name}..."):
            try:
                status, detalhe = process_uploaded_file(f, force=force_reprocess)
            except Exception as e:
                status, detalhe = "erro", str(e)
        icon = {"concluido": "✅", "revisao": "⚠️", "erro": "❌", "duplicado": "↩️"}.get(status, "•")
        st.write(f"{icon} **{f.name}** — {detalhe}")
        progress.progress((i + 1) / len(uploaded_files))
    st.success("Processamento concluído.")

st.divider()

col1, col2 = st.columns([3, 1])
with col1:
    st.subheader("Dados")
with col2:
    if st.button("🔄 Tentar resolver fila pendente"):
        resolvidos = retry_pending_queue()
        if resolvidos:
            st.success(f"Resolvido(s): {', '.join(resolvidos)}")
        else:
            st.info("Nada foi resolvido — ainda faltam documentos correspondentes.")

tab1, tab2, tab3, tab4 = st.tabs(["tb_auditorias", "tb_nao_conformidades", "tb_respostas", "fila pendente / revisão"])

with tab1:
    st.dataframe(db.fetch_all(sb, "tb_auditorias"), use_container_width=True)

with tab2:
    st.dataframe(db.fetch_all(sb, "tb_nao_conformidades"), use_container_width=True)

with tab3:
    st.dataframe(db.fetch_all(sb, "tb_respostas"), use_container_width=True)

with tab4:
    pendentes = [
        a for a in db.fetch_all(sb, "tb_arquivos_processados")
        if a["status"] in ("pendente_vinculo", "parcial", "revisao_manual", "erro")
    ]
    st.dataframe(pendentes, use_container_width=True)
    st.caption(
        "pendente_vinculo / parcial: aguardando a auditoria ou o item correspondente ser cadastrado — "
        "clique em 'Tentar resolver fila pendente' depois de subir o documento que falta. "
        "revisao_manual / erro: precisa de conferência humana."
    )