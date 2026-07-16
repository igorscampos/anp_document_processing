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


def classify_and_extract(texto, tipo_sei=None):
    prompt = pipeline.build_prompt(texto, tipo_sei=tipo_sei)
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
def finalize_result(result, file_hash, nome_arquivo, decisao_catalogo=None, numero_override=None):
    """Aplica overrides determinísticos, o gate de confiança e grava o resultado.

    Compartilhado entre o processamento síncrono (upload direto, 1 chamada de
    API por arquivo) e a aplicação de resultados de um lote da Batch API — a
    única diferença entre os dois fluxos é como "result" (JSON já classificado
    pela IA) chegou até aqui; a partir daqui a lógica de vínculo é idêntica.
    """
    tipo = result["tipo_documento"]

    # Override determinístico: o catálogo do SEI já classificou este documento
    # como Documento de Fiscalização (ou peça oficial equivalente) — não
    # depende da IA acertar isso, e ignora o gate de confiança abaixo.
    if decisao_catalogo == "prioritario":
        tipo = result["tipo_documento"] = "auditoria_oficial"

    # Override determinístico: número de processo ANP já resolvido (regex no
    # texto, ou fallback do nome da pasta no processamento em lote) — garante
    # que documentos do mesmo processo sempre casem entre si.
    if numero_override:
        result["numero_processo_anp"] = numero_override

    if decisao_catalogo != "prioritario" and (result["confianca"] < CONFIDENCE_THRESHOLD or tipo in ("indeterminado", "evidencia_anexa")):
        db.upsert_arquivo_status(sb, file_hash, nome_arquivo, tipo, result["confianca"], "revisao_manual", "confiança baixa ou tipo não processável automaticamente")
        return "revisao", f"confiança {result['confianca']:.2f} — tipo {tipo}"

    processo_label = f" [processo {result.get('numero_processo_anp') or '—'}]"

    if tipo == "auditoria_oficial":
        id_auditoria, n = apply_auditoria(result, nome_arquivo)
        db.upsert_arquivo_status(sb, file_hash, nome_arquivo, tipo, result["confianca"], "concluido", f"auditoria {id_auditoria}")
        resolvidos = retry_pending_queue()
        detalhe = f"auditoria {id_auditoria} atualizada com {n} não conformidade(s){processo_label}"
        if resolvidos:
            detalhe += f" — também resolveu da fila: {', '.join(resolvidos)}"
        return "concluido", detalhe

    # resposta_operadora ou parecer_anp
    status, pendentes, msg = try_apply_respostas(
        result.get("numero_processo_anp"), result.get("numero_relatorio"),
        result.get("respostas", []), tipo, nome_arquivo,
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
    db.upsert_arquivo_status(sb, file_hash, nome_arquivo, tipo, result["confianca"], status, msg, payload)
    status_label = {"concluido": "concluido", "parcial": "revisao", "pendente_vinculo": "revisao"}[status]
    return status_label, msg


def process_uploaded_file(uploaded_file, catalogo=None, force=False):
    file_bytes = uploaded_file.getvalue()
    file_hash = hashlib.sha256(file_bytes).hexdigest()

    existing = db.already_processed(sb, file_hash)
    if existing and existing["status"] in ("concluido", "revisao_manual", "erro", "descartado") and not force:
        return "duplicado", f"já processado antes ({existing['status']}) — marque 'forçar reprocessamento' para refazer"

    # Catálogo do SEI (CSV): decide ANTES de gastar tokens se o arquivo deve
    # ser descartado (erro de captura ou tipo sem conteúdo de auditoria), ou
    # se já sabemos com certeza que é o Documento de Fiscalização (ou peça
    # oficial equivalente) — nesse caso o tipo final é forçado, não depende
    # da IA acertar a classificação.
    doc_id = doc_id_from_filename(uploaded_file.name)
    entry = (catalogo or {}).get(doc_id)
    decisao_catalogo = classify_catalog_entry(entry)

    if decisao_catalogo == "descartar":
        motivo = (
            f"descartado pelo catálogo SEI — tipo '{entry['tipo_documento']}'"
            if entry and entry["status"].strip().lower() == "sucesso"
            else f"descartado pelo catálogo SEI — status de captura '{entry['status']}'" if entry
            else "descartado"
        )
        db.upsert_arquivo_status(sb, file_hash, uploaded_file.name, entry.get("tipo_documento") if entry else None, 1.0, "descartado", motivo)
        return "descartado", motivo

    texto = extract_text(file_bytes)
    if not texto.strip():
        db.upsert_arquivo_status(sb, file_hash, uploaded_file.name, None, 0.0, "erro", "sem texto extraível (pode precisar de OCR)")
        return "erro", "sem texto extraível (pode precisar de OCR)"

    tipo_sei = entry["tipo_documento"] if entry else None
    result = classify_and_extract(texto, tipo_sei=tipo_sei)
    numero_regex = extract_processo_regex(texto)

    return finalize_result(result, file_hash, uploaded_file.name, decisao_catalogo, numero_override=numero_regex)


# ---------------------------------------------------------------------------
# Processamento em lote (ZIP + Batch API) — funcionalidade nova, independente
# do upload direto acima. Cada pasta de 1º nível dentro do ZIP é 1 processo de
# auditoria; todos os PDFs "processáveis" (após o filtro do catálogo) do ZIP
# inteiro viram uma única submissão à Batch API da Anthropic — metade do
# custo por token do processamento síncrono, em troca de latência maior
# (a Batch API normalmente termina em menos de 1h, mas pode levar até 24h).
# ---------------------------------------------------------------------------
def _iter_zip_pdfs(zip_bytes):
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
            # usa a pasta MAIS PRÓXIMA do arquivo como "processo" — funciona
            # tanto para "processo/arquivo.pdf" quanto para o aninhamento
            # duplicado comum em exports de zip ("processo/processo/arquivo.pdf")
            pasta_processo = caminho.parent.name or "sem_pasta"
            yield pasta_processo, caminho.name, zf.read(info)


def prepare_batch_from_zip(zip_bytes, catalogo, force=False):
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
        existing = db.already_processed(sb, file_hash)
        if existing and existing["status"] in ("concluido", "revisao_manual", "erro", "descartado") and not force:
            contagem["duplicado"] += 1
            continue

        doc_id = doc_id_from_filename(nome_arquivo)
        entry = catalogo.get(doc_id)
        decisao = classify_catalog_entry(entry)

        if decisao == "descartar":
            motivo = (
                f"descartado pelo catálogo SEI — tipo '{entry['tipo_documento']}'"
                if entry and entry["status"].strip().lower() == "sucesso"
                else f"descartado pelo catálogo SEI — status de captura '{entry['status']}'" if entry
                else "descartado"
            )
            db.upsert_arquivo_status(sb, file_hash, nome_arquivo, entry.get("tipo_documento") if entry else None, 1.0, "descartado", motivo)
            contagem["descartado"] += 1
            continue

        texto = extract_text(file_bytes)
        if not texto.strip():
            db.upsert_arquivo_status(sb, file_hash, nome_arquivo, None, 0.0, "erro", "sem texto extraível (pode precisar de OCR)")
            contagem["erro"] += 1
            continue

        tipo_sei = entry["tipo_documento"] if entry else None
        prompt = pipeline.build_prompt(texto, tipo_sei=tipo_sei)

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
            # só guardamos o número já resolvido (regex/pasta), não o texto
            # inteiro do documento — a Batch API pode levar horas para
            # responder e não faz sentido inchar o banco com isso.
            "numero_processo_regex": extract_processo_regex(texto),
            "numero_processo_pasta": pipeline.extract_processo_from_foldername(pasta_processo),
        }
        contagem["enviado"] += 1

    return batch_requests, itens, contagem


def submit_batch(batch_requests, itens):
    if not batch_requests:
        return None
    batch = _claude_client.messages.batches.create(requests=batch_requests)
    db.upsert_lote_batch(sb, batch.id, batch.processing_status, len(itens), itens)
    return batch.id


def apply_batch_result(item, result):
    """Aplica o resultado de UM item de um lote já finalizado (`ended`)."""
    nome_arquivo = item["nome_arquivo"]
    file_hash = item["hash_arquivo"]
    decisao_catalogo = item["decisao_catalogo"]
    numero_override = item.get("numero_processo_regex") or item.get("numero_processo_pasta")

    if result.result.type != "succeeded":
        motivo = f"lote: item {result.result.type}"
        db.upsert_arquivo_status(sb, file_hash, nome_arquivo, item.get("tipo_sei"), 0.0, "erro", motivo)
        return "erro", motivo

    raw = "".join(b.text for b in result.result.message.content if b.type == "text")
    try:
        parsed = json.loads(_clean_json_text(raw))
    except json.JSONDecodeError as e:
        motivo = f"lote: resposta da IA não é JSON válido ({e})"
        db.upsert_arquivo_status(sb, file_hash, nome_arquivo, item.get("tipo_sei"), 0.0, "erro", motivo)
        return "erro", motivo

    return finalize_result(parsed, file_hash, nome_arquivo, decisao_catalogo, numero_override=numero_override)


def process_pending_batches():
    """Verifica todos os lotes ainda não concluídos e aplica os resultados prontos."""
    resumo = []
    for lote in db.fetch_lotes_batch(sb, status_excluir="concluido"):
        batch = _claude_client.messages.batches.retrieve(lote["id_lote"])
        if batch.processing_status != "ended":
            db.upsert_lote_batch(sb, lote["id_lote"], batch.processing_status, lote["total_itens"], lote["itens_json"])
            resumo.append(f"{lote['id_lote']}: ainda '{batch.processing_status}'")
            continue

        itens = lote["itens_json"] or {}
        contagem = {}
        for result in _claude_client.messages.batches.results(lote["id_lote"]):
            item = itens.get(result.custom_id)
            if not item:
                continue
            status_label, _ = apply_batch_result(item, result)
            contagem[status_label] = contagem.get(status_label, 0) + 1

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
    "Catálogo de tipos de documento do SEI (CSV: nº processo sei, documento, tipo_documento, data_captura, status) — opcional, mas evita gastar tokens com e-mail/recibo/anexo e garante que o Documento de Fiscalização seja sempre reconhecido",
    type=["csv"],
)
catalogo = {}
if catalogo_csv is not None:
    catalogo = pipeline.load_catalog(io.StringIO(catalogo_csv.getvalue().decode("utf-8-sig")))
    st.caption(f"Catálogo carregado: {len(catalogo)} documento(s) mapeado(s).")

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
                status, detalhe = process_uploaded_file(f, catalogo=catalogo, force=force_reprocess)
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
    "Envie um .zip com uma pasta por processo de auditoria (cada pasta com os PDFs daquele processo). "
    "Usa a Batch API da Anthropic — metade do custo por token, resultado sai em até 24h (geralmente bem menos)."
)

if LLM_PROVIDER != "claude":
    st.info(
        "Processamento em lote via Batch API só está disponível com LLM_PROVIDER = 'claude' "
        "(a Batch API é um recurso da Anthropic, sem equivalente no Ollama)."
    )
else:
    zip_file = st.file_uploader("ZIP com as pastas de processo", type=["zip"], key="zip_lote")
    force_reprocess_lote = st.checkbox(
        "Forçar reprocessamento no lote (mesmo se já processado antes)",
        key="force_lote",
    )

    if zip_file is not None and st.button("Preparar e enviar lote", type="primary"):
        if not catalogo:
            st.warning("Envie o catálogo CSV do SEI acima antes de montar o lote — sem ele, nada é descartado/priorizado.")
        else:
            with st.spinner("Lendo o ZIP e montando as requisições..."):
                batch_requests, itens, contagem = prepare_batch_from_zip(zip_file.getvalue(), catalogo, force=force_reprocess_lote)
            st.write(
                f"📦 {contagem['enviado']} arquivo(s) para a IA · "
                f"🗑️ {contagem['descartado']} descartado(s) pelo catálogo · "
                f"↩️ {contagem['duplicado']} já processado(s) antes · "
                f"❌ {contagem['erro']} sem texto extraível"
            )
            if batch_requests:
                with st.spinner("Enviando lote para a Batch API..."):
                    id_lote = submit_batch(batch_requests, itens)
                st.success(f"Lote enviado: `{id_lote}` — volte aqui mais tarde para verificar o resultado.")
            else:
                st.info("Nenhum arquivo precisou da IA neste ZIP — nada foi enviado à Batch API.")

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
        with st.spinner("Consultando a Batch API..."):
            resumo = process_pending_batches()
        if resumo:
            for linha in resumo:
                st.write(f"• {linha}")
        else:
            st.info("Nenhum lote pendente.")

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