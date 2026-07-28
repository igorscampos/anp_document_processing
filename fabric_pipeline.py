"""Pipeline de processamento de auditorias ANP — versão Fabric (100% nativo, sem UI).

Porte da lógica de `streamlit_app.py` original: tudo que não era `st.*` (chamadas de LLM +
orquestração de vínculo entre auditoria/não-conformidade/resposta/evidência) foi movido pra cá
quase sem alteração. O que muda:

- `_call_ollama` (HTTP local) vira `_call_fabric_ai`, usando o accessor `.ai` do pandas
  registrado por `synapse.ml.aifunc` (Fabric AI Functions) — roda na própria capacidade
  Fabric, sem API key separada, e devolve JSON já validado contra um schema Pydantic
  (`response_format`), então não precisa do "_clean_json_text" que o caminho Claude ainda usa.
- O gatilho de upload do Streamlit vira `scan_landing_zone()`, que varre `Files/entrada/` no
  Lakehouse.
- `st.spinner`/`st.progress`/`st.write` viram `print` — a saída da célula do notebook (ou o
  log da run do Data Pipeline) é o novo "log de auditoria" no lugar da UI.
- `process_uploaded_file` (wrapper em cima de `UploadedFile` do Streamlit) e o checkbox global
  de "forçar reprocessamento" foram removidos — não fazem sentido num pipeline agendado e não
  supervisionado. "Marcar processo pra reprocessar" continua existindo (`tb_processos_reprocessar`),
  documentado no README como uma ação de INSERT direto no banco.

Entrada única pra rodar dentro de um Notebook Fabric (ou de um Data Pipeline que chama esse
notebook): `run_pipeline()`.
"""

import hashlib
import io
import json
import os
import pathlib
import zipfile

from anthropic import Anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request
from pydantic import BaseModel, Field
from typing import List, Literal, Optional

import db
import pipeline


# ---------------------------------------------------------------------------
# Configuração — em produção, a célula inicial do notebook Fabric deve
# popular estas variáveis de ambiente a partir da Variable Library (config
# não-secreta) e de uma conexão Fabric ligada ao Key Vault (ANTHROPIC_API_KEY),
# ex: `os.environ["ANTHROPIC_API_KEY"] = notebookutils.credentials.getSecret(...)`,
# ANTES de importar este módulo. Localmente, um .env/variáveis de ambiente
# comuns bastam pra testar a lógica fora do Fabric.
# ---------------------------------------------------------------------------
def get_config(name, default=None):
    return os.environ.get(name, default)


LLM_PROVIDER = get_config("LLM_PROVIDER", "claude").strip().lower()  # "claude" ou "fabric_ai"
ANTHROPIC_API_KEY = get_config("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = get_config("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
CLAUDE_MAX_CHARS = int(get_config("CLAUDE_MAX_CHARS", "80000"))
# gpt-5-mini (padrão do Fabric AI Functions) tem uma janela de contexto bem maior que a usada
# hoje pro Claude, mas o limite continua existindo pra controlar custo de capacidade por
# chamada, não só pra caber na janela.
FABRIC_AI_MAX_CHARS = int(get_config("FABRIC_AI_MAX_CHARS", "120000"))
CONFIDENCE_THRESHOLD = float(get_config("CONFIDENCE_THRESHOLD", "0.7"))

MAX_CHARS = FABRIC_AI_MAX_CHARS if LLM_PROVIDER == "fabric_ai" else CLAUDE_MAX_CHARS
CURRENT_MODEL_LABEL = "fabric_ai (Fabric AI Functions)" if LLM_PROVIDER == "fabric_ai" else f"claude:{ANTHROPIC_MODEL}"
LANDING_ZONE = os.path.join(db.LAKEHOUSE_FILES_ROOT, "entrada")

_claude_client = None
if LLM_PROVIDER == "claude":
    _claude_client = Anthropic(api_key=ANTHROPIC_API_KEY)


def extract_text_paginado(file_bytes, max_chars=None):
    """(texto_plano, texto_marcado): texto_plano é usado pra regex/checagem de texto vazio;
    texto_marcado tem "[página N]" entre páginas e é o que vai pro prompt da IA."""
    paginas = pipeline.extract_pages(file_bytes)
    limite = max_chars or MAX_CHARS
    texto_plano = "\n".join(paginas)[:limite]
    texto_marcado = pipeline.montar_texto_paginado(paginas, limite)
    return texto_plano, texto_marcado


# ---------------------------------------------------------------------------
# Modelos Pydantic — só usados no caminho fabric_ai, como `response_format` de
# `ai.generate_response`. Espelham o formato JSON documentado em
# `pipeline.PROMPT`/`PROMPT_REMATCH`/`PROMPT_ANEXO_EVIDENCIA`.
# ---------------------------------------------------------------------------
class EvidenciaModel(BaseModel):
    descricao: str
    apresentado_por: Optional[Literal["anp", "operadora"]] = None
    sistema: Optional[str] = None
    equipamento: Optional[str] = None
    categoria: Optional[str] = None
    trecho_original: Optional[str] = None
    pagina_aproximada: Optional[int] = None


class NaoConformidadeModel(BaseModel):
    numero_item: Optional[str] = None
    descricao: Optional[str] = None
    categoria_nao_conformidade: Optional[str] = None
    norma_referencia: Optional[str] = None
    classificacao_gravidade: Optional[str] = None
    acao_recomendada: Optional[str] = None
    prazo_correcao: Optional[str] = None
    evidencias: List[EvidenciaModel] = Field(default_factory=list)


class RespostaModel(BaseModel):
    numero_item: Optional[str] = None
    resultado_final: Optional[str] = None
    decisao_anp: Optional[str] = None
    resumo: Optional[str] = None
    data_resposta: Optional[str] = None
    evidencias: List[EvidenciaModel] = Field(default_factory=list)


class AuditoriaModel(BaseModel):
    codigo_auditoria_anp: Optional[str] = None
    ordem_servico: Optional[str] = None
    operadora: Optional[str] = None
    cnpj_operadora: Optional[str] = None
    unidade_instalacao: Optional[str] = None
    tipo_auditoria: Optional[str] = None
    sumario_auditoria: Optional[str] = None
    acao_demandada: Optional[str] = None
    data_auditoria_inicio: Optional[str] = None
    data_auditoria_fim: Optional[str] = None
    data_emissao_relatorio: Optional[str] = None
    auditor_responsavel: Optional[str] = None
    auditores_equipe: Optional[str] = None
    status_auditoria: Optional[str] = None
    resultado_final: Optional[str] = None


class ClassificacaoExtracaoModel(BaseModel):
    tipo_documento: str
    confianca: float
    numero_processo_anp: Optional[str] = None
    numero_relatorio: Optional[str] = None
    data_emissao_documento: Optional[str] = None
    auditoria: Optional[AuditoriaModel] = None
    nao_conformidades: List[NaoConformidadeModel] = Field(default_factory=list)
    respostas: List[RespostaModel] = Field(default_factory=list)


class RematchResultModel(BaseModel):
    numero_item: Optional[str] = None


class EvidenciasAnexoModel(BaseModel):
    evidencias: List[EvidenciaModel] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Camada de LLM — troque LLM_PROVIDER na config pra alternar entre os dois
# ---------------------------------------------------------------------------
def _clean_json_text(raw):
    """Só usado no caminho Claude — a Messages API da Anthropic aqui não tem saída
    estruturada, então o texto às vezes vem com cerca de markdown em volta do JSON."""
    raw = raw.strip().strip("`")
    if raw.lower().startswith("json"):
        raw = raw[4:].strip()
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


def _call_fabric_ai(prompt, response_model):
    try:
        import synapse.ml.aifunc as aifunc  # noqa: F401 — registra o accessor .ai em DataFrames pandas
    except ImportError as e:
        raise RuntimeError(
            "synapse.ml.aifunc só está disponível no runtime de notebook do Fabric, com "
            "capacidade paga F2+ e o tenant switch de Copilot/Azure OpenAI habilitado."
        ) from e
    import pandas as pd

    df = pd.DataFrame([{"texto": prompt}])
    respostas = df.ai.generate_response("{texto}", is_prompt_template=True, response_format=response_model)
    return respostas.iloc[0]


def _call_llm_json(prompt, response_model):
    """Chama o provedor configurado e devolve o dict já parseado.

    No caminho fabric_ai, `response_format` garante JSON conforme `response_model` — não
    precisa de `_clean_json_text`. No caminho Claude, mantém o parsing de texto livre de
    sempre, já que a Messages API usada aqui não tem saída estruturada.
    """
    if LLM_PROVIDER == "fabric_ai":
        raw = _call_fabric_ai(prompt, response_model)
        return json.loads(raw)
    raw = _call_claude(prompt)
    return json.loads(_clean_json_text(raw))


def classify_and_extract(texto, tipo_sei=None, nc_existentes=None):
    prompt = pipeline.build_prompt(texto, tipo_sei=tipo_sei, nao_conformidades_existentes=nc_existentes)
    return _call_llm_json(prompt, ClassificacaoExtracaoModel)


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
        return _call_llm_json(prompt, RematchResultModel).get("numero_item")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Lógica de vínculo — reutilizada tanto no scan da pasta de entrada quanto na
# fila de retentativa
# ---------------------------------------------------------------------------
def apply_auditoria(result, filename, is_df=False, link_arquivo=None, data_emissao=None):
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
    id_auditoria = db.upsert_auditoria(aud, filename, data_emissao=data_emissao)
    n = 0
    if is_df:
        for nc in result.get("nao_conformidades", []):
            db.upsert_nc(id_auditoria, nc, filename, link_arquivo=link_arquivo, data_emissao=data_emissao)
        n = len(result.get("nao_conformidades", []))
        # Quando há mais de um DF no mesmo processo, o mais informativo (mais
        # não conformidades + mais campos de auditoria preenchidos) deve ficar
        # como arquivo_origem de referência — não importa a ordem de chegada.
        score = 100 * n + len(aud)
        db.atualizar_arquivo_origem_df(id_auditoria, filename, score)
    return id_auditoria, n


def try_apply_respostas(numero_processo_anp, numero_relatorio, respostas, tipo, filename, link_arquivo=None, data_emissao=None):
    """Tenta vincular cada resposta a uma não conformidade existente.

    Retorna (status, itens_nao_resolvidos, mensagem).
    status: 'concluido' | 'parcial' | 'pendente_vinculo'
    """
    aud = db.find_auditoria(numero_processo_anp, numero_relatorio)
    if not aud:
        return "pendente_vinculo", respostas, "auditoria ainda não cadastrada"

    resolvidos, pendentes = 0, []
    ncs_cache = None
    for r in respostas:
        nc = db.find_nc(aud["id_auditoria"], r.get("numero_item"))
        if not nc:
            # numero_item não bateu exatamente — o documento pode usar uma
            # numeração própria (ex: sua própria seção de análise), sem
            # relação com a numeração das notificações do DF. Pergunta pra
            # IA, só com o resumo já extraído (sem reprocessar o PDF), qual
            # NC real corresponde ao assunto.
            if ncs_cache is None:
                ncs_cache = db.fetch_ncs_resumo(aud["id_auditoria"])
            numero_recasado = match_resposta_to_nc(r, ncs_cache)
            if numero_recasado:
                nc = db.find_nc(aud["id_auditoria"], numero_recasado)
        if not nc:
            pendentes.append(r)
            continue
        db.insert_resposta(nc["id_nao_conformidade"], aud["id_auditoria"], tipo, r, filename, link_arquivo=link_arquivo, data_emissao=data_emissao)
        resolvidos += 1

    if not pendentes:
        return "concluido", [], f"{resolvidos} resposta(s) vinculada(s) à auditoria {aud['id_auditoria']}"
    if resolvidos == 0:
        return "pendente_vinculo", pendentes, "item(ns) de não conformidade ainda não cadastrado(s)"
    return "parcial", pendentes, f"{resolvidos} vinculada(s), {len(pendentes)} aguardando item correspondente"


def retry_pending_queue():
    """Roda depois de toda auditoria nova: tenta resolver a fila de pendências."""
    resolvidos = []
    for item in db.fetch_pendentes():
        payload = item["payload_json"] or {}
        status, pendentes, msg = try_apply_respostas(
            payload.get("numero_processo_anp"),
            payload.get("numero_relatorio"),
            payload.get("respostas", []),
            payload.get("tipo_documento"),
            item["nome_arquivo"],
            link_arquivo=payload.get("link_arquivo"),
            data_emissao=payload.get("data_emissao"),
        )
        if status != item["status"] or len(pendentes) != len(payload.get("respostas", [])):
            novo_payload = {**payload, "respostas": pendentes} if pendentes else None
            db.upsert_arquivo_status(
                item["hash_arquivo"], item["nome_arquivo"], item["tipo_documento"],
                item["confianca"], status, msg, novo_payload,
            )
            if status == "concluido":
                resolvidos.append(item["nome_arquivo"])
    return resolvidos


# ---------------------------------------------------------------------------
# Processamento de um arquivo
# ---------------------------------------------------------------------------
def finalize_result(result, file_hash, nome_arquivo, decisao_catalogo=None, numero_override=None, is_df=False,
                     numero_sei=None, numero_processo_divergente=None, link_arquivo=None):
    """Aplica overrides determinísticos, o gate de confiança e grava o resultado.

    Compartilhado entre o processamento de um arquivo solto, a fila síncrona
    do lote fabric_ai e a aplicação de resultados de um lote da Batch API — a
    única diferença entre os três fluxos é como "result" (JSON já classificado
    pela IA) chegou até aqui; a partir daqui a lógica de vínculo é idêntica
    pros três.
    """
    tipo = result["tipo_documento"]
    data_emissao = pipeline.normalize_data_iso(result.get("data_emissao_documento"))

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
        db.upsert_arquivo_status(file_hash, nome_arquivo, tipo, result["confianca"], "revisao_manual", "confiança baixa ou tipo não processável automaticamente", data_emissao_documento=data_emissao, **rastreio)
        return "revisao", f"confiança {result['confianca']:.2f} — tipo {tipo}"

    processo_label = f" [processo {result.get('numero_processo_anp') or '—'}]"

    if tipo == "auditoria_oficial":
        id_auditoria, n = apply_auditoria(result, nome_arquivo, is_df=is_df, link_arquivo=link_arquivo, data_emissao=data_emissao)
        db.upsert_arquivo_status(file_hash, nome_arquivo, tipo, result["confianca"], "concluido", f"auditoria {id_auditoria}", data_emissao_documento=data_emissao, **rastreio)
        resolvidos = retry_pending_queue()
        detalhe = f"auditoria {id_auditoria} atualizada com {n} não conformidade(s){processo_label}"
        if resolvidos:
            detalhe += f" — também resolveu da fila: {', '.join(resolvidos)}"
        return "concluido", detalhe

    # resposta_operadora ou parecer_anp
    status, pendentes, msg = try_apply_respostas(
        result.get("numero_processo_anp"), result.get("numero_relatorio"),
        result.get("respostas", []), tipo, nome_arquivo, link_arquivo=link_arquivo, data_emissao=data_emissao,
    )
    msg += processo_label
    # Salva o payload sempre que NÃO resolveu de vez (mesmo se "pendentes"
    # vier vazio) — um parecer sem nenhuma resposta extraída ainda precisa
    # do numero_processo_anp guardado pra fila de pendências achar a
    # auditoria depois; se só salvássemos quando "pendentes" é não-vazio,
    # esse arquivo perderia pra sempre a referência do processo e nunca
    # mais seria resolvido automaticamente.
    payload = None
    if status != "concluido":
        payload = {
            "tipo_documento": tipo,
            "numero_processo_anp": result.get("numero_processo_anp"),
            "numero_relatorio": result.get("numero_relatorio"),
            "respostas": pendentes,
            "link_arquivo": link_arquivo,
            "data_emissao": data_emissao,
        }
    db.upsert_arquivo_status(file_hash, nome_arquivo, tipo, result["confianca"], status, msg, payload, data_emissao_documento=data_emissao, **rastreio)
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
    ncs_esperando = db.fetch_evidencias_por_anexo(doc_id)
    if not ncs_esperando:
        return None

    texto_anexo, texto_anexo_marcado = extract_text_paginado(file_bytes)
    if not texto_anexo.strip():
        return None

    contexto_por_nc = {}
    for row in ncs_esperando:
        contexto_por_nc.setdefault(row["id_nao_conformidade"], []).append(row.get("descricao"))

    link_anexo = db.upload_evidencia_arquivo(file_bytes, file_hash, nome_arquivo)
    total_evidencias = 0
    for id_nc, descricoes in contexto_por_nc.items():
        contexto_nc = " | ".join(d for d in descricoes if d)
        prompt = pipeline.build_anexo_evidencia_prompt(contexto_nc, texto_anexo_marcado)
        try:
            evidencias = _call_llm_json(prompt, EvidenciasAnexoModel).get("evidencias", [])
        except Exception:
            continue
        db.insert_evidencias(evidencias, nome_arquivo, id_nao_conformidade=id_nc, link_arquivo=link_anexo)
        total_evidencias += len(evidencias)

    motivo = f"anexo referenciado pelo DF — {total_evidencias} evidência(s) extraída(s) para {len(contexto_por_nc)} não conformidade(s)"
    db.upsert_arquivo_status(
        file_hash, nome_arquivo, "Anexo", 1.0, "concluido", motivo,
        numero_sei=doc_id, numero_processo_anp=numero_catalogo, link_arquivo=link_anexo,
    )
    return "concluido", motivo


def process_one_file(file_bytes, nome_arquivo, catalogo=None, numero_pasta_fallback=None, processos_reprocessar=None):
    """Processa 1 arquivo já em memória (bytes) — chamada de IA síncrona.

    Núcleo compartilhado entre PDFs soltos na pasta de entrada e a fila
    síncrona do lote fabric_ai (onde os bytes vêm do zipfile).
    `numero_pasta_fallback` só é usado pelo caminho do ZIP — é o número de
    processo lido do nome da pasta, para quando o próprio PDF não tem o
    rodapé padrão do SEI.

    `processos_reprocessar` é o conjunto (já resolvido ANTES do laço de
    arquivos, uma vez só) de números de processo marcados para forçar
    releitura — se o processo deste arquivo estiver nele, força o
    reprocessamento deste arquivo, e remove a marca (é um "disparo só"; como
    o conjunto já foi lido antes do laço, os outros arquivos do MESMO
    processo nesta mesma leva continuam vendo a marca normalmente, mesmo
    depois dela ser removida do banco aqui).
    """
    file_hash = hashlib.sha256(file_bytes).hexdigest()

    # Catálogo do SEI (CSV) é a fonte de verdade obrigatória: decide ANTES de
    # gastar tokens se o arquivo deve ser descartado (erro de captura, tipo
    # sem conteúdo de auditoria, OU simplesmente não mapeado no CSV), se já
    # sabemos com certeza que é o Documento de Fiscalização (ou peça oficial
    # equivalente), e qual é o número de processo autoritativo dele.
    doc_id = pipeline.doc_id_from_filename(nome_arquivo)
    entry = (catalogo or {}).get(doc_id)
    decisao_catalogo = pipeline.classify_catalog_entry(entry)
    numero_catalogo = pipeline.normalize_processo(entry["processo"]) if entry and entry.get("processo") else None

    force = False
    if numero_catalogo and processos_reprocessar and numero_catalogo in processos_reprocessar:
        force = True
        db.desmarcar_processo_reprocessar(numero_catalogo)

    existing = db.already_processed(file_hash)
    if existing and existing["status"] in ("concluido", "revisao_manual", "erro", "descartado") and not force:
        return "duplicado", f"já processado antes ({existing['status']})"

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
            file_hash, nome_arquivo, entry.get("tipo_documento") if entry else None, 1.0, "descartado", motivo,
            numero_sei=doc_id, numero_processo_anp=numero_catalogo,
        )
        return "descartado", motivo

    texto, texto_marcado = extract_text_paginado(file_bytes)
    if not texto.strip():
        db.upsert_arquivo_status(
            file_hash, nome_arquivo, None, 0.0, "erro", "sem texto extraível (pode precisar de OCR)",
            numero_sei=doc_id, numero_processo_anp=numero_catalogo,
        )
        return "erro", "sem texto extraível (pode precisar de OCR)"

    tipo_sei = entry["tipo_documento"] if entry else None
    is_df = pipeline.is_tipo_df(tipo_sei)

    # Se este documento NÃO é da família do DF (é resposta/parecer em
    # potencial) e já existe auditoria cadastrada pro processo, manda a lista
    # de não conformidades já registradas junto no prompt — permite a IA
    # casar pelo ASSUNTO em vez de depender de o documento repetir a mesma
    # numeração do DF.
    nc_existentes = None
    if not is_df and numero_catalogo:
        aud_existente = db.find_auditoria(numero_processo_anp=numero_catalogo)
        if aud_existente:
            nc_existentes = db.fetch_ncs_resumo(aud_existente["id_auditoria"])

    result = classify_and_extract(texto_marcado, tipo_sei=tipo_sei, nc_existentes=nc_existentes)

    # Prioridade do número de processo: catálogo (fonte de verdade) sempre
    # ganha; um número diferente encontrado no texto/pasta vira divergência,
    # nunca é usado pro vínculo.
    numero_texto = pipeline.extract_processo_regex(texto) or numero_pasta_fallback
    numero_divergente = None
    if numero_catalogo:
        numero_override = numero_catalogo
        if numero_texto and pipeline.normalize_processo(numero_texto) != numero_catalogo:
            numero_divergente = numero_texto
    else:
        numero_override = numero_texto  # sem entrada no catálogo — não deveria mais ocorrer, mantém defensivo

    link_arquivo = db.upload_evidencia_arquivo(file_bytes, file_hash, nome_arquivo)

    return finalize_result(
        result, file_hash, nome_arquivo, decisao_catalogo, numero_override=numero_override, is_df=is_df,
        numero_sei=doc_id, numero_processo_divergente=numero_divergente, link_arquivo=link_arquivo,
    )


# ---------------------------------------------------------------------------
# Processamento em lote (ZIP dropado em Files/entrada/) — dois caminhos de
# execução, escolhidos pelo LLM_PROVIDER:
#   - claude: Batch API da Anthropic — todos os PDFs "processáveis" do ZIP
#     inteiro viram uma única submissão, metade do custo por token, em troca
#     de latência maior (normalmente termina em menos de 1h, mas pode levar
#     até 24h — os resultados só são aplicados numa run futura do pipeline).
#   - fabric_ai: sem equivalente de Batch API, então processa em fila
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


def prepare_batch_from_zip(zip_bytes, catalogo, processos_reprocessar=None):
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

        doc_id = pipeline.doc_id_from_filename(nome_arquivo)
        entry = catalogo.get(doc_id)
        decisao = pipeline.classify_catalog_entry(entry)
        numero_catalogo = pipeline.normalize_processo(entry["processo"]) if entry and entry.get("processo") else None

        force_este = False
        if numero_catalogo and processos_reprocessar and numero_catalogo in processos_reprocessar:
            force_este = True
            db.desmarcar_processo_reprocessar(numero_catalogo)

        existing = db.already_processed(file_hash)
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
                file_hash, nome_arquivo, entry.get("tipo_documento") if entry else None, 1.0, "descartado", motivo,
                numero_sei=doc_id, numero_processo_anp=numero_catalogo,
            )
            contagem["descartado"] += 1
            continue

        texto, texto_marcado = extract_text_paginado(file_bytes)
        if not texto.strip():
            db.upsert_arquivo_status(
                file_hash, nome_arquivo, None, 0.0, "erro", "sem texto extraível (pode precisar de OCR)",
                numero_sei=doc_id, numero_processo_anp=numero_catalogo,
            )
            contagem["erro"] += 1
            continue

        tipo_sei = entry["tipo_documento"] if entry else None
        is_df_item = pipeline.is_tipo_df(tipo_sei)

        nc_existentes = None
        if not is_df_item and numero_catalogo:
            aud_existente = db.find_auditoria(numero_processo_anp=numero_catalogo)
            if aud_existente:
                nc_existentes = db.fetch_ncs_resumo(aud_existente["id_auditoria"])

        prompt = pipeline.build_prompt(texto_marcado, tipo_sei=tipo_sei, nao_conformidades_existentes=nc_existentes)

        # Prioridade do número de processo: catálogo (fonte de verdade)
        # sempre ganha; divergência do texto/pasta só é registrada, não usada.
        numero_texto = pipeline.extract_processo_regex(texto) or pipeline.extract_processo_from_foldername(pasta_processo)
        numero_divergente = None
        if numero_catalogo:
            numero_final = numero_catalogo
            if numero_texto and pipeline.normalize_processo(numero_texto) != numero_catalogo:
                numero_divergente = numero_texto
        else:
            numero_final = numero_texto

        link_arquivo = db.upload_evidencia_arquivo(file_bytes, file_hash, nome_arquivo)

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


def process_zip_batch_sync(zip_bytes, catalogo, on_item=None, processos_reprocessar=None):
    """Processa um ZIP em fila síncrona: chama a IA arquivo por arquivo,
    aguardando cada resposta antes de seguir pro próximo, agrupado por
    processo (todos os arquivos de um processo antes de passar ao próximo).

    Único caminho disponível pro fabric_ai, que não tem Batch API — mas
    também serve pra rodar um ZIP com Claude sem usar a Batch API, se
    preferir resultado imediato em vez de esperar o lote.

    "on_item" (opcional) é chamado a cada arquivo processado com
    (pasta_processo, nome_arquivo, status, detalhe) — usado pra logar o
    progresso na saída da célula do notebook.
    """
    itens = sorted(_iter_zip_pdfs(zip_bytes), key=lambda t: (t[0], t[1]))
    contagem = {}
    for pasta_processo, nome_arquivo, file_bytes in itens:
        numero_pasta = pipeline.extract_processo_from_foldername(pasta_processo)
        try:
            status, detalhe = process_one_file(
                file_bytes, nome_arquivo, catalogo=catalogo,
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
    db.upsert_lote_batch(batch.id, batch.processing_status, len(itens), itens, nome_arquivo_zip=nome_arquivo_zip)
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
            file_hash, nome_arquivo, item.get("tipo_sei"), 0.0, "erro", motivo,
            numero_sei=item.get("numero_sei"), numero_processo_anp=numero_override,
        )
        return "erro", motivo

    raw = "".join(b.text for b in result.result.message.content if b.type == "text")
    try:
        parsed = json.loads(_clean_json_text(raw))
    except json.JSONDecodeError as e:
        motivo = f"lote: resposta da IA não é JSON válido ({e})"
        db.upsert_arquivo_status(
            file_hash, nome_arquivo, item.get("tipo_sei"), 0.0, "erro", motivo,
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
    vira só um "erro" registrado naquele arquivo, em vez de derrubar a
    verificação inteira e quebrar a run do pipeline.
    """
    resumo = []
    for lote in db.fetch_lotes_batch(status_excluir="concluido"):
        try:
            batch = _claude_client.messages.batches.retrieve(lote["id_lote"])
        except Exception as e:
            resumo.append(f"{lote['id_lote']}: erro ao consultar o lote — {e}")
            continue

        if batch.processing_status != "ended":
            db.upsert_lote_batch(lote["id_lote"], batch.processing_status, lote["total_itens"], lote["itens_json"])
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
                db.upsert_arquivo_status(item["hash_arquivo"], item["nome_arquivo"], item.get("tipo_sei"), 0.0, "erro", f"lote: falha ao aplicar resultado — {e}")
                status_label = "erro"
            contagem[status_label] = contagem.get(status_label, 0) + 1
            if on_item:
                on_item(item["nome_arquivo"], status_label, i + 1, total)

        db.upsert_lote_batch(lote["id_lote"], "concluido", lote["total_itens"], itens)
        resumo.append(
            f"{lote['id_lote']}: concluído — {contagem.get('concluido', 0)} ok, "
            f"{contagem.get('revisao', 0)} revisão, {contagem.get('erro', 0)} erro"
        )
    return resumo


# ---------------------------------------------------------------------------
# Varredura da pasta de entrada (OneLake) — substitui o gatilho de upload do
# Streamlit
# ---------------------------------------------------------------------------
def scan_landing_zone():
    """Varre `Files/entrada/` uma vez: localiza o catálogo CSV do SEI, PDFs
    soltos e quaisquer `.zip` de lote, e processa tudo pelo pipeline
    existente.

    Arquivos já processados NÃO são movidos dali de propósito — o dedup por
    hash em tb_arquivos_processados já torna reprocessar um arquivo idêntico
    um no-op sem gastar token; mover criaria uma segunda fonte de verdade
    (estado do filesystem) que pode divergir do banco sem ganho de correção.
    Revisitar só se o volume da pasta crescer a ponto do próprio scan (que já
    é barato — um SELECT indexado por hash por arquivo) ficar lento.
    """
    if not os.path.isdir(LANDING_ZONE):
        print(f"Pasta de entrada não encontrada: {LANDING_ZONE}")
        return {"erro": "pasta de entrada ausente"}

    catalogo, csv_path = {}, None
    loose_pdfs, zip_paths = [], []
    for entry in os.scandir(LANDING_ZONE):
        if entry.is_dir():
            continue
        nome = entry.name.lower()
        if nome.endswith(".csv"):
            csv_path = entry.path
        elif nome.endswith(".pdf"):
            loose_pdfs.append(entry.path)
        elif nome.endswith(".zip"):
            zip_paths.append(entry.path)

    if csv_path:
        with open(csv_path, encoding="utf-8-sig") as f:
            catalogo = pipeline.load_catalog(f)
    if not catalogo:
        print(
            "Nenhum catálogo CSV do SEI encontrado em Files/entrada/ — todos os arquivos "
            "seriam descartados sem ele (fonte de verdade obrigatória). Abortando o scan."
        )
        return {"erro": "catalogo ausente"}

    processos_reprocessar = db.fetch_processos_reprocessar()
    resultados = {"concluido": 0, "revisao": 0, "erro": 0, "duplicado": 0, "descartado": 0}

    for pdf_path in loose_pdfs:
        with open(pdf_path, "rb") as f:
            file_bytes = f.read()
        try:
            status, detalhe = process_one_file(
                file_bytes, os.path.basename(pdf_path), catalogo=catalogo, processos_reprocessar=processos_reprocessar,
            )
        except Exception as e:
            status, detalhe = "erro", str(e)
        resultados[status] = resultados.get(status, 0) + 1
        print(f"{pdf_path}: {status} — {detalhe}")

    for zip_path in zip_paths:
        with open(zip_path, "rb") as f:
            zip_bytes = f.read()
        nome_zip = os.path.basename(zip_path)

        if LLM_PROVIDER == "claude":
            batch_requests, itens, contagem = prepare_batch_from_zip(zip_bytes, catalogo, processos_reprocessar=processos_reprocessar)
            for chave, valor in contagem.items():
                resultados[chave] = resultados.get(chave, 0) + valor
            if batch_requests:
                id_lote = submit_batch(batch_requests, itens, nome_arquivo_zip=nome_zip)
                print(f"{zip_path}: lote {id_lote} enviado ({contagem['enviado']} item(ns)) — resultado aplicado numa run futura")
            else:
                print(f"{zip_path}: nada precisou da IA, nenhum lote enviado")
        else:
            def _on_item(pasta, nome, status, detalhe):
                print(f"  [{pasta}] {nome}: {status} — {detalhe}")

            contagem = process_zip_batch_sync(zip_bytes, catalogo, on_item=_on_item, processos_reprocessar=processos_reprocessar)
            for chave, valor in contagem.items():
                resultados[chave] = resultados.get(chave, 0) + valor

    return resultados


# ---------------------------------------------------------------------------
# Entrada única do notebook / Data Pipeline agendado
# ---------------------------------------------------------------------------
def run_pipeline():
    """Ordem importa: (1) aplica lotes da Batch API já concluídos de runs
    anteriores — resolve pendências antes de qualquer coisa nova ser
    enfileirada; (2) resolve a fila de pendências explicitamente (cobre o
    caso de itens que ficaram pendentes por outro motivo que não a Batch
    API); (3) varre a pasta de entrada por arquivos novos — pra ZIPs via
    Claude Batch API isso só ENVIA o lote, a aplicação do resultado acontece
    numa run futura, de volta no passo 1.
    """
    print(f"== Pipeline ANP (Fabric) — provedor: {CURRENT_MODEL_LABEL} ==")

    print("-- 1/3: aplicando lotes da Batch API já concluídos --")
    for linha in process_pending_batches():
        print(f"  {linha}")

    print("-- 2/3: resolvendo fila de pendências --")
    resolvidos = retry_pending_queue()
    print(f"  resolvido(s): {', '.join(resolvidos)}" if resolvidos else "  nada a resolver")

    print("-- 3/3: varrendo pasta de entrada por arquivos novos --")
    resultados = scan_landing_zone()
    print(f"  resumo: {resultados}")
    return resultados
