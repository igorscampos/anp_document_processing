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
import pathlib
import zipfile

import streamlit as st
from anthropic import Anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

import db
import pipeline
from pipeline import (
    extract_processo_regex,
    normalize_processo,
    extract_text as _pipeline_extract_text,
    doc_id_from_filename,
    classify_catalog_entry,
    load_catalog,
)

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
OLLAMA_NUM_PREDICT = int(get_secret("OLLAMA_NUM_PREDICT", "4096"))
# Modelos de "raciocínio" (qwen3, qwen3.5, deepseek-r1 etc.) mandam a resposta
# inteira para o campo "thinking" do Ollama e deixam "response" vazio quando o
# pensamento não é desligado — o JSON nunca chegava a ser gerado no campo que
# o código lia. "think: false" faz o modelo responder direto no campo certo.
OLLAMA_THINK = get_secret("OLLAMA_THINK", "false").strip().lower() in ("true", "1", "yes")

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

def extract_text(file_bytes, max_chars=None):
    return _pipeline_extract_text(file_bytes, max_chars or MAX_CHARS)


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
            "think": OLLAMA_THINK,
            "options": {"temperature": 0, "num_ctx": OLLAMA_NUM_CTX, "num_predict": OLLAMA_NUM_PREDICT},
        },
        timeout=300,
    )
    resp.raise_for_status()
    data = resp.json()
    texto = data.get("response") or ""
    if not texto.strip():
        # Modelo de raciocínio "vazou" tudo pro campo thinking mesmo com
        # think=false (acontece se o Ollama instalado for antigo e ignorar
        # o parâmetro) — melhor avisar claramente do que devolver um erro
        # genérico de JSON inválido.
        pista = " (o modelo pensou mas não respondeu — tente atualizar o Ollama ou trocar de modelo)" if data.get("thinking") else ""
        raise ValueError(f"Ollama devolveu resposta vazia para o modelo '{OLLAMA_MODEL}'{pista}.")
    return texto


def classify_and_extract(texto, tipo_sei=None, nc_existentes=None):
    prompt = pipeline.build_prompt(texto, tipo_sei=tipo_sei, nao_conformidades_existentes=nc_existentes)
    raw = _call_ollama(prompt) if LLM_PROVIDER == "ollama" else _call_claude(prompt)
    return json.loads(_clean_json_text(raw))


def match_resposta_to_nc(r, ncs):
    """Quando o numero_item extraído não bate com nenhuma NC cadastrada (o
    documento pode usar uma numeração própria, sem relação com a do DF),
    pergunta à IA — com um prompt pequeno, sem reprocessar o PDF inteiro —
    qual NC real corresponde ao assunto já extraído. Retorna o numero_item
    escolhido, ou None se não achar correspondência ou a chamada falhar.
    """
    if not ncs or not (r.get("resumo") or r.get("decisao_anp")):
        return None
    prompt = pipeline.build_rematch_prompt(r, ncs)
    try:
        raw = _call_ollama(prompt) if LLM_PROVIDER == "ollama" else _call_claude(prompt)
        return json.loads(_clean_json_text(raw)).get("numero_item")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Lógica de vínculo — reutilizada tanto no upload quanto na fila de retentativa
# ---------------------------------------------------------------------------
def apply_auditoria(result, filename, is_df=False, link_arquivo=None):
    """auditoria_oficial sempre "resolve" na hora: cria ou atualiza.

    Não conformidades só são gravadas quando "is_df" — o documento é o
    Documento de Fiscalização em si. Ordem de Serviço, Plano e Relatório
    também chegam aqui como auditoria_oficial (enriquecem tb_auditorias),
    mas nunca devem gerar linha em tb_nao_conformidades: essa é
    exclusivamente do DF (ou de vários DFs, se o processo tiver mais de um —
    todos contribuem, upsert_nc já mescla por numero_item).
    """
    aud = {k: v for k, v in {
        "numero_processo_anp": result.get("numero_processo_anp"),
        "numero_relatorio": result.get("numero_relatorio"),
        **(result.get("auditoria") or {}),
    }.items() if v is not None}
    id_auditoria = db.upsert_auditoria(sb, aud, filename)
    n = 0
    if is_df:
        for nc in result.get("nao_conformidades", []):
            db.upsert_nc(sb, id_auditoria, nc, filename, link_arquivo=link_arquivo)
        n = len(result.get("nao_conformidades", []))
        # Quando há mais de um DF no mesmo processo, o mais informativo (mais
        # não conformidades + mais campos de auditoria preenchidos) deve ficar
        # como arquivo_origem de referência — não importa a ordem de chegada.
        score = 100 * n + len(aud)
        db.atualizar_arquivo_origem_df(sb, id_auditoria, filename, score)
    return id_auditoria, n


def try_apply_respostas(numero_processo_anp, numero_relatorio, respostas, tipo, filename, link_arquivo=None):
    """Tenta vincular cada resposta a uma não conformidade existente.

    Retorna (status, itens_nao_resolvidos, mensagem).
    status: 'concluido' | 'parcial' | 'pendente_vinculo'
    """
    aud = db.find_auditoria(sb, numero_processo_anp, numero_relatorio)
    if not aud:
        return "pendente_vinculo", respostas, "auditoria ainda não cadastrada"

    resolvidos, pendentes = 0, []
    ncs_cache = None
    for r in respostas:
        nc = db.find_nc(sb, aud["id_auditoria"], r.get("numero_item"))
        if not nc:
            # numero_item não bateu exatamente — o documento pode usar uma
            # numeração própria (ex: sua própria seção de análise), sem
            # relação com a numeração das notificações do DF. Pergunta pra
            # IA, só com o resumo já extraído (sem reprocessar o PDF), qual
            # NC real corresponde ao assunto.
            if ncs_cache is None:
                ncs_cache = db.fetch_ncs_resumo(sb, aud["id_auditoria"])
            numero_recasado = match_resposta_to_nc(r, ncs_cache)
            if numero_recasado:
                nc = db.find_nc(sb, aud["id_auditoria"], numero_recasado)
        if not nc:
            pendentes.append(r)
            continue
        db.insert_resposta(sb, nc["id_nao_conformidade"], aud["id_auditoria"], tipo, r, filename, link_arquivo=link_arquivo)
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
            link_arquivo=payload.get("link_arquivo"),
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
def finalize_result(result, file_hash, nome_arquivo, decisao_catalogo=None, numero_override=None, is_df=False,
                     numero_sei=None, numero_processo_divergente=None, link_arquivo=None):
    """Aplica overrides determinísticos, o gate de confiança e grava o resultado.

    Compartilhado entre o processamento síncrono (upload direto, 1 chamada de
    API por arquivo), a fila síncrona do ZIP e a aplicação de resultados de um
    lote da Batch API — a única diferença entre os três fluxos é como
    "result" (JSON já classificado pela IA) chegou até aqui; a partir daqui a
    lógica de vínculo é idêntica pros três.
    """
    tipo = result["tipo_documento"]

    # Override determinístico: o catálogo do SEI já classificou este documento
    # como Documento de Fiscalização (ou peça oficial equivalente) — não
    # depende da IA acertar isso, e ignora o gate de confiança abaixo.
    if decisao_catalogo == "prioritario":
        tipo = result["tipo_documento"] = "auditoria_oficial"

    # Override determinístico: número de processo ANP vem do catálogo CSV
    # (fonte de verdade) — garante que documentos do mesmo processo sempre
    # casem entre si, mesmo que o texto/pasta sugira um número diferente
    # (esse caso vira numero_processo_divergente, só pra auditoria).
    if numero_override:
        result["numero_processo_anp"] = numero_override

    rastreio = dict(
        numero_sei=numero_sei,
        numero_processo_anp=result.get("numero_processo_anp"),
        numero_processo_divergente=numero_processo_divergente,
    )

    if decisao_catalogo != "prioritario" and (result["confianca"] < CONFIDENCE_THRESHOLD or tipo in ("indeterminado", "evidencia_anexa")):
        db.upsert_arquivo_status(sb, file_hash, nome_arquivo, tipo, result["confianca"], "revisao_manual", "confiança baixa ou tipo não processável automaticamente", **rastreio)
        return "revisao", f"confiança {result['confianca']:.2f} — tipo {tipo}"

    processo_label = f" [processo {result.get('numero_processo_anp') or '—'}]"

    if tipo == "auditoria_oficial":
        id_auditoria, n = apply_auditoria(result, nome_arquivo, is_df=is_df, link_arquivo=link_arquivo)
        db.upsert_arquivo_status(sb, file_hash, nome_arquivo, tipo, result["confianca"], "concluido", f"auditoria {id_auditoria}", **rastreio)
        resolvidos = retry_pending_queue()
        detalhe = f"auditoria {id_auditoria} atualizada com {n} não conformidade(s){processo_label}"
        if resolvidos:
            detalhe += f" — também resolveu da fila: {', '.join(resolvidos)}"
        return "concluido", detalhe

    # resposta_operadora ou parecer_anp
    status, pendentes, msg = try_apply_respostas(
        result.get("numero_processo_anp"), result.get("numero_relatorio"),
        result.get("respostas", []), tipo, nome_arquivo, link_arquivo=link_arquivo,
    )
    msg += processo_label
    # Salva o payload sempre que NÃO resolveu de vez (mesmo se "pendentes"
    # vier vazio) — um parecer sem nenhuma resposta extraída ainda precisa
    # do numero_processo_anp guardado pra fila de pendências achar a
    # auditoria depois; se só salvássemos quando "pendentes" é não-vazio,
    # esse arquivo perderia pra sempre a referência do processo e nunca
    # mais seria resolvido automaticamente (só reprocessando na mão).
    payload = None
    if status != "concluido":
        payload = {
            "tipo_documento": tipo,
            "numero_processo_anp": result.get("numero_processo_anp"),
            "numero_relatorio": result.get("numero_relatorio"),
            "respostas": pendentes,
            "link_arquivo": link_arquivo,
        }
    db.upsert_arquivo_status(sb, file_hash, nome_arquivo, tipo, result["confianca"], status, msg, payload, **rastreio)
    status_label = {"concluido": "concluido", "parcial": "revisao", "pendente_vinculo": "revisao"}[status]
    return status_label, msg


def _try_processar_anexo_referenciado(file_bytes, file_hash, nome_arquivo, doc_id, numero_catalogo):
    """Só chamada para tipo 'Anexo' (nunca E-mail/Recibo/outros descartados).

    Se algum DF já citou este número SEI como fonte de mais detalhe de uma
    não conformidade (`fetch_evidencias_por_anexo`), lê o anexo, extrai
    evidências específicas com um prompt pequeno (sem reclassificar o
    documento) e grava com o link do PRÓPRIO anexo — não o do DF. Sem
    citação pendente, devolve None e o chamador descarta normalmente.
    """
    ncs_esperando = db.fetch_evidencias_por_anexo(sb, doc_id)
    if not ncs_esperando:
        return None

    texto_anexo = extract_text(file_bytes)
    if not texto_anexo.strip():
        return None

    contexto_por_nc = {}
    for row in ncs_esperando:
        contexto_por_nc.setdefault(row["id_nao_conformidade"], []).append(row.get("descricao"))

    link_anexo = db.upload_evidencia_arquivo(sb, file_bytes, file_hash, nome_arquivo)
    total_evidencias = 0
    for id_nc, descricoes in contexto_por_nc.items():
        contexto_nc = " | ".join(d for d in descricoes if d)
        prompt = pipeline.build_anexo_evidencia_prompt(contexto_nc, texto_anexo)
        try:
            raw = _call_ollama(prompt) if LLM_PROVIDER == "ollama" else _call_claude(prompt)
            evidencias = json.loads(_clean_json_text(raw)).get("evidencias", [])
        except Exception:
            continue
        db.insert_evidencias(sb, evidencias, nome_arquivo, id_nao_conformidade=id_nc, link_arquivo=link_anexo)
        total_evidencias += len(evidencias)

    motivo = f"anexo referenciado pelo DF — {total_evidencias} evidência(s) extraída(s) para {len(contexto_por_nc)} não conformidade(s)"
    db.upsert_arquivo_status(
        sb, file_hash, nome_arquivo, "Anexo", 1.0, "concluido", motivo,
        numero_sei=doc_id, numero_processo_anp=numero_catalogo, link_arquivo=link_anexo,
    )
    return "concluido", motivo


def process_one_file(file_bytes, nome_arquivo, catalogo=None, force=False, numero_pasta_fallback=None,
                      processos_reprocessar=None):
    """Processa 1 arquivo já em memória (bytes) — chamada de IA síncrona.

    Núcleo compartilhado entre o upload direto (um `UploadedFile` do
    Streamlit) e a fila síncrona do processamento em lote via ZIP (onde os
    bytes vêm do zipfile, sem objeto de upload). `numero_pasta_fallback` só é
    usado pelo caminho do ZIP — é o número de processo lido do nome da pasta,
    para quando o próprio PDF não tem o rodapé padrão do SEI.

    `processos_reprocessar` é o conjunto (já resolvido ANTES do laço de
    arquivos, uma vez só) de números de processo marcados para forçar
    releitura — se o processo deste arquivo estiver nele, força o
    reprocessamento deste arquivo mesmo com o checkbox global desmarcado, e
    remove a marca (é um "disparo só"; como o conjunto já foi lido antes do
    laço, os outros arquivos do MESMO processo nesta mesma leva continuam
    vendo a marca normalmente, mesmo depois dela ser removida do banco aqui).
    """
    file_hash = hashlib.sha256(file_bytes).hexdigest()

    # Catálogo do SEI (CSV) é a fonte de verdade obrigatória: decide ANTES de
    # gastar tokens se o arquivo deve ser descartado (erro de captura, tipo
    # sem conteúdo de auditoria, OU simplesmente não mapeado no CSV), se já
    # sabemos com certeza que é o Documento de Fiscalização (ou peça oficial
    # equivalente), e qual é o número de processo autoritativo dele.
    doc_id = doc_id_from_filename(nome_arquivo)
    entry = (catalogo or {}).get(doc_id)
    decisao_catalogo = classify_catalog_entry(entry)
    numero_catalogo = normalize_processo(entry["processo"]) if entry and entry.get("processo") else None

    if numero_catalogo and processos_reprocessar and numero_catalogo in processos_reprocessar:
        force = True
        db.desmarcar_processo_reprocessar(sb, numero_catalogo)

    existing = db.already_processed(sb, file_hash)
    if existing and existing["status"] in ("concluido", "revisao_manual", "erro", "descartado") and not force:
        return "duplicado", f"já processado antes ({existing['status']}) — marque 'forçar reprocessamento' para refazer"

    if decisao_catalogo == "descartar":
        if entry and pipeline.is_tipo_anexo(entry.get("tipo_documento")):
            resultado_anexo = _try_processar_anexo_referenciado(file_bytes, file_hash, nome_arquivo, doc_id, numero_catalogo)
            if resultado_anexo:
                return resultado_anexo
        motivo = (
            f"descartado pelo catálogo SEI — tipo '{entry['tipo_documento']}'"
            if entry and entry["status"].strip().lower() == "sucesso"
            else f"descartado pelo catálogo SEI — status de captura '{entry['status']}'" if entry
            else "não mapeado no catálogo CSV"
        )
        db.upsert_arquivo_status(
            sb, file_hash, nome_arquivo, entry.get("tipo_documento") if entry else None, 1.0, "descartado", motivo,
            numero_sei=doc_id, numero_processo_anp=numero_catalogo,
        )
        return "descartado", motivo

    texto = extract_text(file_bytes)
    if not texto.strip():
        db.upsert_arquivo_status(
            sb, file_hash, nome_arquivo, None, 0.0, "erro", "sem texto extraível (pode precisar de OCR)",
            numero_sei=doc_id, numero_processo_anp=numero_catalogo,
        )
        return "erro", "sem texto extraível (pode precisar de OCR)"

    tipo_sei = entry["tipo_documento"] if entry else None
    is_df = pipeline.is_tipo_df(tipo_sei)

    # Se este documento NÃO é da família do DF (é resposta/parecer em
    # potencial) e já existe auditoria cadastrada pro processo, manda a lista
    # de não conformidades já registradas junto no prompt — permite a IA
    # casar pelo ASSUNTO em vez de depender de o documento repetir a mesma
    # numeração do DF (documento pode ter numeração própria, ex: "2.X" numa
    # seção de análise interna, sem relação com o "5.X" do DF).
    nc_existentes = None
    if not is_df and numero_catalogo:
        aud_existente = db.find_auditoria(sb, numero_processo_anp=numero_catalogo)
        if aud_existente:
            nc_existentes = db.fetch_ncs_resumo(sb, aud_existente["id_auditoria"])

    result = classify_and_extract(texto, tipo_sei=tipo_sei, nc_existentes=nc_existentes)

    # Prioridade do número de processo: catálogo (fonte de verdade) sempre
    # ganha; um número diferente encontrado no texto/pasta vira divergência,
    # nunca é usado pro vínculo.
    numero_texto = extract_processo_regex(texto) or numero_pasta_fallback
    numero_divergente = None
    if numero_catalogo:
        numero_override = numero_catalogo
        if numero_texto and normalize_processo(numero_texto) != numero_catalogo:
            numero_divergente = numero_texto
    else:
        numero_override = numero_texto  # sem entrada no catálogo — não deveria mais ocorrer, mantém defensivo

    link_arquivo = db.upload_evidencia_arquivo(sb, file_bytes, file_hash, nome_arquivo)

    return finalize_result(
        result, file_hash, nome_arquivo, decisao_catalogo, numero_override=numero_override, is_df=is_df,
        numero_sei=doc_id, numero_processo_divergente=numero_divergente, link_arquivo=link_arquivo,
    )


def process_uploaded_file(uploaded_file, catalogo=None, force=False, processos_reprocessar=None):
    return process_one_file(
        uploaded_file.getvalue(), uploaded_file.name, catalogo=catalogo, force=force,
        processos_reprocessar=processos_reprocessar,
    )


# ---------------------------------------------------------------------------
# Processamento em lote (ZIP) — funcionalidade nova, independente do upload
# direto acima. Cada pasta de 1º nível dentro do ZIP é 1 processo de
# auditoria (os arquivos podem estar soltos nessa pasta ou em subpastas
# dentro dela — tudo que estiver sob a mesma pasta de 1º nível conta pro
# mesmo processo). Dois caminhos de execução, escolhidos pelo LLM_PROVIDER:
#   - claude: Batch API da Anthropic — todos os PDFs "processáveis" do ZIP
#     inteiro viram uma única submissão, metade do custo por token, em troca
#     de latência maior (normalmente termina em menos de 1h, mas pode levar
#     até 24h).
#   - ollama: sem equivalente de Batch API local, então processa em fila
#     síncrona — chama a IA arquivo por arquivo, aguardando cada resposta
#     antes de seguir pro próximo, agrupado por processo.
# ---------------------------------------------------------------------------
def _iter_zip_pdfs(zip_bytes):
    """Percorre o ZIP inteiro, inclusive subpastas dentro da pasta de cada
    processo. O processo é identificado pelo PRIMEIRO segmento do caminho
    dentro do zip — não importa quantos níveis de subpasta o PDF esteja.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            caminho = pathlib.PurePosixPath(info.filename)
            if caminho.suffix.lower() != ".pdf":
                continue
            # ignora lixo comum de export de zip (metadados do macOS, ocultos)
            if any(parte.startswith(".") or parte == "__MACOSX" for parte in caminho.parts):
                continue
            pasta_processo = caminho.parts[0] if len(caminho.parts) > 1 else "sem_pasta"
            yield pasta_processo, caminho.name, zf.read(info)


def prepare_batch_from_zip(zip_bytes, catalogo, force=False, processos_reprocessar=None):
    """Varre o ZIP e monta as requisições da Batch API.

    Descarta, marca erro ou duplicado imediatamente (sem gastar tokens) — só
    os documentos que realmente precisam da IA entram no lote. Retorna
    (lista_de_requests, itens{custom_id: detalhes}, contagem).
    """
    batch_requests = []
    itens = {}
    contagem = {"descartado": 0, "erro": 0, "duplicado": 0, "enviado": 0}

    for i, (pasta_processo, nome_arquivo, file_bytes) in enumerate(_iter_zip_pdfs(zip_bytes)):
        file_hash = hashlib.sha256(file_bytes).hexdigest()

        doc_id = doc_id_from_filename(nome_arquivo)
        entry = catalogo.get(doc_id)
        decisao = classify_catalog_entry(entry)
        numero_catalogo = normalize_processo(entry["processo"]) if entry and entry.get("processo") else None

        force_este = force
        if numero_catalogo and processos_reprocessar and numero_catalogo in processos_reprocessar:
            force_este = True
            db.desmarcar_processo_reprocessar(sb, numero_catalogo)

        existing = db.already_processed(sb, file_hash)
        if existing and existing["status"] in ("concluido", "revisao_manual", "erro", "descartado") and not force_este:
            contagem["duplicado"] += 1
            continue

        if decisao == "descartar":
            if entry and pipeline.is_tipo_anexo(entry.get("tipo_documento")):
                resultado_anexo = _try_processar_anexo_referenciado(file_bytes, file_hash, nome_arquivo, doc_id, numero_catalogo)
                if resultado_anexo:
                    # não passa pela Batch API — já foi lido e concluído aqui mesmo
                    contagem["anexo_lido"] = contagem.get("anexo_lido", 0) + 1
                    continue
            motivo = (
                f"descartado pelo catálogo SEI — tipo '{entry['tipo_documento']}'"
                if entry and entry["status"].strip().lower() == "sucesso"
                else f"descartado pelo catálogo SEI — status de captura '{entry['status']}'" if entry
                else "não mapeado no catálogo CSV"
            )
            db.upsert_arquivo_status(
                sb, file_hash, nome_arquivo, entry.get("tipo_documento") if entry else None, 1.0, "descartado", motivo,
                numero_sei=doc_id, numero_processo_anp=numero_catalogo,
            )
            contagem["descartado"] += 1
            continue

        texto = extract_text(file_bytes)
        if not texto.strip():
            db.upsert_arquivo_status(
                sb, file_hash, nome_arquivo, None, 0.0, "erro", "sem texto extraível (pode precisar de OCR)",
                numero_sei=doc_id, numero_processo_anp=numero_catalogo,
            )
            contagem["erro"] += 1
            continue

        tipo_sei = entry["tipo_documento"] if entry else None
        is_df_item = pipeline.is_tipo_df(tipo_sei)

        # Mesmo raciocínio do caminho síncrono: se não é DF e já existe
        # auditoria pro processo, manda as NCs já cadastradas no prompt pra
        # IA casar pelo assunto, não pela numeração própria do documento.
        nc_existentes = None
        if not is_df_item and numero_catalogo:
            aud_existente = db.find_auditoria(sb, numero_processo_anp=numero_catalogo)
            if aud_existente:
                nc_existentes = db.fetch_ncs_resumo(sb, aud_existente["id_auditoria"])

        prompt = pipeline.build_prompt(texto, tipo_sei=tipo_sei, nao_conformidades_existentes=nc_existentes)

        # Prioridade do número de processo: catálogo (fonte de verdade)
        # sempre ganha; divergência do texto/pasta só é registrada, não usada.
        numero_texto = extract_processo_regex(texto) or pipeline.extract_processo_from_foldername(pasta_processo)
        numero_divergente = None
        if numero_catalogo:
            numero_final = numero_catalogo
            if numero_texto and normalize_processo(numero_texto) != numero_catalogo:
                numero_divergente = numero_texto
        else:
            numero_final = numero_texto

        link_arquivo = db.upload_evidencia_arquivo(sb, file_bytes, file_hash, nome_arquivo)

        custom_id = f"item{i:05d}_{file_hash[:8]}"
        batch_requests.append(Request(
            custom_id=custom_id,
            params=MessageCreateParamsNonStreaming(
                model=ANTHROPIC_MODEL,
                max_tokens=8000,
                messages=[{"role": "user", "content": prompt}],
            ),
        ))
        itens[custom_id] = {
            "nome_arquivo": nome_arquivo,
            "hash_arquivo": file_hash,
            "pasta_processo": pasta_processo,
            "tipo_sei": tipo_sei,
            "decisao_catalogo": decisao,
            "is_df": is_df_item,
            "numero_sei": doc_id,
            # só guardamos strings já resolvidas, não o texto inteiro do
            # documento — a Batch API pode levar horas para responder e não
            # faz sentido inchar o banco com isso.
            "numero_processo_final": numero_final,
            "numero_processo_divergente": numero_divergente,
            "link_arquivo": link_arquivo,
        }
        contagem["enviado"] += 1

    return batch_requests, itens, contagem


def process_zip_batch_sync(zip_bytes, catalogo, force=False, on_item=None, processos_reprocessar=None):
    """Processa um ZIP em fila síncrona: chama a IA arquivo por arquivo,
    aguardando cada resposta antes de seguir pro próximo, agrupado por
    processo (todos os arquivos de um processo antes de passar ao próximo).

    Único caminho disponível pra Ollama, que não tem Batch API — mas também
    serve pra rodar um ZIP com Claude sem usar a Batch API, se preferir
    resultado imediato em vez de esperar o lote.

    "on_item" (opcional) é chamado a cada arquivo processado com
    (pasta_processo, nome_arquivo, status, detalhe) — usado pra atualizar a
    UI item a item.
    """
    itens = sorted(_iter_zip_pdfs(zip_bytes), key=lambda t: (t[0], t[1]))
    contagem = {}
    for pasta_processo, nome_arquivo, file_bytes in itens:
        numero_pasta = pipeline.extract_processo_from_foldername(pasta_processo)
        try:
            status, detalhe = process_one_file(
                file_bytes, nome_arquivo, catalogo=catalogo, force=force,
                numero_pasta_fallback=numero_pasta, processos_reprocessar=processos_reprocessar,
            )
        except Exception as e:
            status, detalhe = "erro", str(e)
        contagem[status] = contagem.get(status, 0) + 1
        if on_item:
            on_item(pasta_processo, nome_arquivo, status, detalhe)
    return contagem


def submit_batch(batch_requests, itens, nome_arquivo_zip=None):
    if not batch_requests:
        return None
    batch = _claude_client.messages.batches.create(requests=batch_requests)
    db.upsert_lote_batch(sb, batch.id, batch.processing_status, len(itens), itens, nome_arquivo_zip=nome_arquivo_zip)
    return batch.id


def apply_batch_result(item, result):
    """Aplica o resultado de UM item de um lote já finalizado (`ended`)."""
    nome_arquivo = item["nome_arquivo"]
    file_hash = item["hash_arquivo"]
    decisao_catalogo = item["decisao_catalogo"]
    numero_override = item.get("numero_processo_final")

    if result.result.type != "succeeded":
        motivo = f"lote: item {result.result.type}"
        db.upsert_arquivo_status(
            sb, file_hash, nome_arquivo, item.get("tipo_sei"), 0.0, "erro", motivo,
            numero_sei=item.get("numero_sei"), numero_processo_anp=numero_override,
        )
        return "erro", motivo

    raw = "".join(b.text for b in result.result.message.content if b.type == "text")
    try:
        parsed = json.loads(_clean_json_text(raw))
    except json.JSONDecodeError as e:
        motivo = f"lote: resposta da IA não é JSON válido ({e})"
        db.upsert_arquivo_status(
            sb, file_hash, nome_arquivo, item.get("tipo_sei"), 0.0, "erro", motivo,
            numero_sei=item.get("numero_sei"), numero_processo_anp=numero_override,
        )
        return "erro", motivo

    return finalize_result(
        parsed, file_hash, nome_arquivo, decisao_catalogo, numero_override=numero_override,
        is_df=item.get("is_df", False), numero_sei=item.get("numero_sei"),
        numero_processo_divergente=item.get("numero_processo_divergente"), link_arquivo=item.get("link_arquivo"),
    )


def process_pending_batches(on_item=None):
    """Verifica todos os lotes ainda não concluídos e aplica os resultados prontos.

    Cada item e cada lote são isolados em try/except — um resultado com
    problema (ex: JSON com campo que a IA inventou e não existe na tabela)
    vira só um "erro" registrado naquele arquivo, igual ao upload direto, em
    vez de derrubar a verificação inteira e quebrar a página no Streamlit.

    Aplicar um lote grande é lento: cada item leva várias chamadas
    sequenciais ao Supabase (e `retry_pending_queue` reprocessa a fila
    inteira a cada auditoria concluída), então um lote com dezenas de itens
    pode levar minutos nessa função. Sem feedback nenhum, um clique nessa
    função parece travado no meio do caminho — "on_item" (opcional) é
    chamado a cada item aplicado com (nome_arquivo, status_label, indice,
    total) pra UI mostrar o progresso real em vez de um spinner mudo.
    """
    resumo = []
    for lote in db.fetch_lotes_batch(sb, status_excluir="concluido"):
        try:
            batch = _claude_client.messages.batches.retrieve(lote["id_lote"])
        except Exception as e:
            resumo.append(f"{lote['id_lote']}: erro ao consultar o lote — {e}")
            continue

        if batch.processing_status != "ended":
            db.upsert_lote_batch(sb, lote["id_lote"], batch.processing_status, lote["total_itens"], lote["itens_json"])
            resumo.append(f"{lote['id_lote']}: ainda '{batch.processing_status}'")
            continue

        itens = lote["itens_json"] or {}
        total = len(itens)
        contagem = {}
        for i, result in enumerate(_claude_client.messages.batches.results(lote["id_lote"])):
            item = itens.get(result.custom_id)
            if not item:
                continue
            try:
                status_label, _ = apply_batch_result(item, result)
            except Exception as e:
                db.upsert_arquivo_status(sb, item["hash_arquivo"], item["nome_arquivo"], item.get("tipo_sei"), 0.0, "erro", f"lote: falha ao aplicar resultado — {e}")
                status_label = "erro"
            contagem[status_label] = contagem.get(status_label, 0) + 1
            if on_item:
                on_item(item["nome_arquivo"], status_label, i + 1, total)

        db.upsert_lote_batch(sb, lote["id_lote"], "concluido", lote["total_itens"], itens)
        resumo.append(
            f"{lote['id_lote']}: concluído — {contagem.get('concluido', 0)} ok, "
            f"{contagem.get('revisao', 0)} revisão, {contagem.get('erro', 0)} erro"
        )
    return resumo


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

catalogo_csv = st.file_uploader(
    "Catálogo de tipos de documento do SEI (CSV: nº processo sei, documento, tipo_documento, data_captura, status) — "
    "OBRIGATÓRIO: é a fonte de verdade do número de processo, e qualquer arquivo fora dele é descartado sem chamar a IA",
    type=["csv"],
)
catalogo = {}
if catalogo_csv is not None:
    try:
        catalogo = pipeline.load_catalog(io.StringIO(catalogo_csv.getvalue().decode("utf-8-sig")))
        st.caption(f"Catálogo carregado: {len(catalogo)} documento(s) mapeado(s).")
    except ValueError as e:
        st.error(str(e))

with st.expander("Marcar processo(s) para forçar reprocessamento"):
    st.caption(
        "Cola um ou mais números de processo (um por linha, ou separados por vírgula). Na próxima vez que os "
        "arquivos desses processos passarem por qualquer um dos fluxos abaixo, são reprocessados mesmo sem marcar "
        "o checkbox — a marca é removida automaticamente depois de usada."
    )
    texto_marcar = st.text_area("Números de processo", key="texto_marcar_reprocessar", label_visibility="collapsed")
    if st.button("Marcar para reprocessar"):
        numeros = [n.strip() for linha in texto_marcar.splitlines() for n in linha.split(",") if n.strip()]
        if numeros:
            db.marcar_processos_reprocessar(sb, numeros)
            st.success(f"{len(numeros)} processo(s) marcado(s).")
    marcados = db.fetch_processos_reprocessar(sb)
    if marcados:
        st.caption(f"Atualmente marcado(s): {', '.join(sorted(marcados))}")

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
    if not catalogo:
        st.error("Envie o catálogo CSV do SEI acima antes de processar — sem ele, todos os arquivos seriam descartados por não estarem mapeados.")
    else:
        processos_reprocessar = db.fetch_processos_reprocessar(sb)
        progress = st.progress(0.0)
        for i, f in enumerate(uploaded_files):
            with st.spinner(f"Processando {f.name}..."):
                try:
                    status, detalhe = process_uploaded_file(f, catalogo=catalogo, force=force_reprocess, processos_reprocessar=processos_reprocessar)
                except Exception as e:
                    status, detalhe = "erro", str(e)
            icon = {"concluido": "✅", "revisao": "⚠️", "erro": "❌", "duplicado": "↩️", "descartado": "🗑️"}.get(status, "•")
            st.write(f"{icon} **{f.name}** — {detalhe}")
            progress.progress((i + 1) / len(uploaded_files))
        st.success("Processamento concluído.")

st.divider()

# ---------------------------------------------------------------------------
# Processamento em lote (ZIP + Batch API) — metade do custo por token,
# em troca de latência (a Batch API pode levar até 24h para terminar,
# geralmente menos de 1h). Não afeta o upload direto acima.
# ---------------------------------------------------------------------------
st.subheader("Processamento em lote (ZIP)")
st.caption(
    "Envie um ou mais .zip, cada um com uma pasta por processo de auditoria — os PDFs daquele processo "
    "podem estar soltos na pasta ou em subpastas dentro dela. Cada .zip enviado vira um lote independente: "
    "um erro em um lote não afeta os demais."
)

zip_files = st.file_uploader(
    "ZIP com as pastas de processo", type=["zip"], key="zip_lote", accept_multiple_files=True,
)
force_reprocess_lote = st.checkbox(
    "Forçar reprocessamento no lote (mesmo se já processado antes)",
    key="force_lote",
)

if LLM_PROVIDER == "claude":
    st.caption("Usa a Batch API da Anthropic — metade do custo por token, resultado sai em até 24h (geralmente bem menos).")

    if zip_files and st.button(f"Preparar e enviar {len(zip_files)} lote(s) (Batch API)", type="primary"):
        if not catalogo:
            st.error("Envie o catálogo CSV do SEI acima antes de montar o lote — sem ele, todos os arquivos seriam descartados por não estarem mapeados.")
        else:
            processos_reprocessar = db.fetch_processos_reprocessar(sb)
            for zip_file in zip_files:
                st.markdown(f"**{zip_file.name}**")
                try:
                    with st.spinner(f"Lendo {zip_file.name} e montando as requisições..."):
                        batch_requests, itens, contagem = prepare_batch_from_zip(
                            zip_file.getvalue(), catalogo, force=force_reprocess_lote, processos_reprocessar=processos_reprocessar,
                        )
                    st.write(
                        f"📦 {contagem['enviado']} arquivo(s) para a IA · "
                        f"🗑️ {contagem['descartado']} descartado(s) pelo catálogo · "
                        f"📎 {contagem.get('anexo_lido', 0)} anexo(s) referenciado(s) lido(s) · "
                        f"↩️ {contagem['duplicado']} já processado(s) antes · "
                        f"❌ {contagem['erro']} sem texto extraível"
                    )
                    if batch_requests:
                        with st.spinner(f"Enviando {zip_file.name} para a Batch API..."):
                            id_lote = submit_batch(batch_requests, itens, nome_arquivo_zip=zip_file.name)
                        st.success(f"Lote enviado: `{id_lote}` ({zip_file.name}) — volte aqui mais tarde para verificar o resultado.")
                    else:
                        st.info(f"Nenhum arquivo de {zip_file.name} precisou da IA — nada foi enviado à Batch API.")
                except Exception as e:
                    st.error(f"Falha ao processar {zip_file.name}: {e} — os demais lotes seguem independentes.")

    st.markdown("**Lotes enviados**")
    lotes = db.fetch_lotes_batch(sb)
    if lotes:
        st.dataframe(
            [{k: v for k, v in l.items() if k != "itens_json"} for l in lotes],
            use_container_width=True,
        )
    else:
        st.caption("Nenhum lote enviado ainda.")

    if st.button("🔄 Verificar / baixar resultados dos lotes pendentes"):
        progress = st.progress(0.0)
        log = st.empty()

        def _on_item_lote(nome_arquivo, status_label, i, total):
            log.write(f"Aplicando resultados do lote... {i}/{total} — {nome_arquivo} ({status_label})")
            progress.progress(i / total)

        with st.spinner("Consultando a Batch API e aplicando resultados (pode levar minutos em lotes grandes)..."):
            resumo = process_pending_batches(on_item=_on_item_lote)
        progress.empty()
        log.empty()
        if resumo:
            for linha in resumo:
                st.write(f"• {linha}")
        else:
            st.info("Nenhum lote pendente.")

else:
    st.caption(
        "Ollama não tem Batch API — o lote é processado em fila síncrona: chama a IA arquivo por "
        "arquivo (agrupado por processo), aguardando cada resposta antes de seguir pro próximo."
    )

    if zip_files and st.button(f"Processar {len(zip_files)} lote(s) agora (fila síncrona)", type="primary"):
        if not catalogo:
            st.error("Envie o catálogo CSV do SEI acima antes de processar o lote — sem ele, todos os arquivos seriam descartados por não estarem mapeados.")
        else:
            processos_reprocessar = db.fetch_processos_reprocessar(sb)
            for zip_file in zip_files:
                with st.expander(f"Lote: {zip_file.name}", expanded=True):
                    try:
                        zip_bytes = zip_file.getvalue()
                        total_itens = sum(1 for _ in _iter_zip_pdfs(zip_bytes))
                        if total_itens == 0:
                            st.info("Nenhum PDF encontrado dentro do ZIP.")
                            continue

                        progress = st.progress(0.0)
                        log = st.container()
                        estado = {"i": 0}

                        def _on_item(pasta_processo, nome_arquivo, status, detalhe):
                            estado["i"] += 1
                            icon = {"concluido": "✅", "revisao": "⚠️", "erro": "❌", "duplicado": "↩️", "descartado": "🗑️"}.get(status, "•")
                            log.write(f"{icon} **[{pasta_processo}] {nome_arquivo}** — {detalhe}")
                            progress.progress(estado["i"] / total_itens)

                        with st.spinner(f"Processando {total_itens} arquivo(s) de {zip_file.name} na fila..."):
                            contagem = process_zip_batch_sync(
                                zip_bytes, catalogo, force=force_reprocess_lote, on_item=_on_item, processos_reprocessar=processos_reprocessar,
                            )
                        st.success(
                            f"{zip_file.name} concluído — {contagem.get('concluido', 0)} ok, {contagem.get('revisao', 0)} revisão, "
                            f"{contagem.get('erro', 0)} erro, {contagem.get('descartado', 0)} descartado(s), "
                            f"{contagem.get('duplicado', 0)} já processado(s) antes."
                        )
                    except Exception as e:
                        st.error(f"Falha ao processar {zip_file.name}: {e} — os demais lotes seguem independentes.")

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

tab1, tab2, tab3, tab4, tab5 = st.tabs(["tb_auditorias", "tb_nao_conformidades", "tb_respostas", "tb_evidencias", "fila pendente / revisão"])

with tab1:
    st.dataframe(db.fetch_all(sb, "tb_auditorias"), use_container_width=True)

with tab2:
    st.dataframe(db.fetch_all(sb, "tb_nao_conformidades"), use_container_width=True)

with tab3:
    st.dataframe(db.fetch_all(sb, "tb_respostas"), use_container_width=True)

with tab4:
    st.dataframe(db.fetch_all(sb, "tb_evidencias"), use_container_width=True)
    st.caption(
        "Cada linha é 1 evidência ligada a UMA não conformidade ou UMA resposta (nunca as duas) — "
        "'apresentado_por' indica se foi a operadora ou a própria ANP quem apresentou."
    )

with tab5:
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