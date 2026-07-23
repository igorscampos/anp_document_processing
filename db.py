"""Funções de acesso ao Supabase (Postgres) para as 4 tabelas do pipeline.

Trata o caso de upload em ordem aleatória: se um documento de resposta/parecer
chega antes da auditoria (ou de uma não conformidade específica) existir, o
resultado já extraído pela IA fica guardado em tb_arquivos_processados
(payload_json) e é retentado automaticamente sempre que uma auditoria nova é
gravada — sem precisar chamar a API de novo.
"""

import re
import uuid

from supabase import create_client


def get_client(url, key):
    return create_client(url, key)


def gen_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _norm(item):
    return (item or "").strip().lower()


def _norm_processo(numero):
    if not numero:
        return numero
    numero = numero.strip()
    for ch in "‐‑‒–—−":
        numero = numero.replace(ch, "-")
    return numero


# ---------------------------------------------------------------------------
# Controle de duplicidade / fila de pendências
# ---------------------------------------------------------------------------
def already_processed(sb, file_hash):
    res = sb.table("tb_arquivos_processados").select("*").eq("hash_arquivo", file_hash).execute()
    return res.data[0] if res.data else None


def upsert_arquivo_status(sb, file_hash, nome_arquivo, tipo_documento, confianca, status, motivo=None, payload_json=None,
                           numero_sei=None, numero_processo_anp=None, numero_processo_divergente=None, link_arquivo=None):
    sb.table("tb_arquivos_processados").upsert({
        "hash_arquivo": file_hash,
        "nome_arquivo": nome_arquivo,
        "tipo_documento": tipo_documento,
        "confianca": confianca,
        "status": status,
        "motivo": motivo,
        "payload_json": payload_json,
        "numero_sei": numero_sei,
        "numero_processo_anp": numero_processo_anp,
        "numero_processo_divergente": numero_processo_divergente,
        "link_arquivo": link_arquivo,
    }).execute()


def fetch_link_arquivo(sb, numero_sei):
    """Link do Storage de um arquivo já processado, buscado pelo número SEI
    (nome do arquivo sem extensão) — usado pra resolver o link de uma
    evidência pro arquivo que realmente tem o detalhe, não pro documento
    principal que só cita esse número."""
    if not numero_sei:
        return None
    res = (
        sb.table("tb_arquivos_processados")
        .select("link_arquivo")
        .eq("numero_sei", numero_sei)
        .not_.is_("link_arquivo", "null")
        .limit(1)
        .execute()
    )
    return res.data[0]["link_arquivo"] if res.data else None


def fetch_pendentes(sb):
    res = (
        sb.table("tb_arquivos_processados")
        .select("*")
        .in_("status", ["pendente_vinculo", "parcial"])
        .execute()
    )
    return res.data


# ---------------------------------------------------------------------------
# tb_lotes_batch — processamento em lote via Batch API da Anthropic
# ---------------------------------------------------------------------------
def upsert_lote_batch(sb, id_lote, status, total_itens, itens_json, nome_arquivo_zip=None):
    payload = {
        "id_lote": id_lote,
        "status": status,
        "total_itens": total_itens,
        "itens_json": itens_json,
    }
    # só inclui se informado — evita apagar o nome já gravado quando esta
    # função é chamada de novo só pra atualizar status (process_pending_batches)
    if nome_arquivo_zip is not None:
        payload["nome_arquivo_zip"] = nome_arquivo_zip
    sb.table("tb_lotes_batch").upsert(payload).execute()


def fetch_lotes_batch(sb, status_excluir=None):
    res = sb.table("tb_lotes_batch").select("*").order("criado_em", desc=True).execute()
    dados = res.data
    if status_excluir:
        dados = [l for l in dados if l["status"] != status_excluir]
    return dados


# ---------------------------------------------------------------------------
# tb_auditorias
# ---------------------------------------------------------------------------
def find_auditoria(sb, numero_processo_anp=None, numero_relatorio=None):
    if numero_processo_anp:
        alvo = _norm_processo(numero_processo_anp)
        res = sb.table("tb_auditorias").select("*").execute()
        for row in res.data:
            if _norm_processo(row.get("numero_processo_anp")) == alvo:
                return row
    if numero_relatorio:
        res = sb.table("tb_auditorias").select("*").ilike("numero_relatorio", f"%{numero_relatorio}%").execute()
        if res.data:
            return res.data[0]
    return None


# Colunas de tb_auditorias que a IA pode preencher (exclui id_auditoria,
# arquivo_origem, criado_em, atualizado_em, que são geridas aqui). "dados"
# vem de um objeto JSON que a própria IA gerou e é espalhado (**dados) direto
# no payload — sem esse filtro, qualquer chave inesperada que o modelo
# inventasse (nome de campo diferente, campo extra) ia direto pro Postgrest e
# quebrava com "column not found", derrubando o processamento inteiro em vez
# de só marcar aquele arquivo como erro.
CAMPOS_AUDITORIA = {
    "numero_processo_anp", "numero_relatorio", "codigo_auditoria_anp", "ordem_servico",
    "operadora", "cnpj_operadora", "unidade_instalacao", "tipo_auditoria",
    "sumario_auditoria", "acao_demandada", "data_auditoria_inicio", "data_auditoria_fim",
    "data_emissao_relatorio", "auditor_responsavel", "auditores_equipe", "status_auditoria",
    "resultado_final",
}


def upsert_auditoria(sb, dados, arquivo_origem):
    dados = {k: v for k, v in dados.items() if k in CAMPOS_AUDITORIA}
    existente = find_auditoria(sb, dados.get("numero_processo_anp"), dados.get("numero_relatorio"))
    if existente:
        id_auditoria = existente["id_auditoria"]
        payload = {k: v for k, v in dados.items() if v is not None}
        if payload:
            sb.table("tb_auditorias").update(payload).eq("id_auditoria", id_auditoria).execute()
    else:
        id_auditoria = gen_id("AUD")
        payload = {"id_auditoria": id_auditoria, "arquivo_origem": arquivo_origem, **dados}
        sb.table("tb_auditorias").insert(payload).execute()
    return id_auditoria


def atualizar_arquivo_origem_df(sb, id_auditoria, arquivo_origem, score):
    """Só troca arquivo_origem (+ arquivo_origem_score) da auditoria se o novo
    score for MAIOR que o já salvo — garante que, quando há mais de um DF no
    mesmo processo, o mais informativo (mais não conformidades e mais campos
    de auditoria preenchidos) fique como referência, não importa a ordem de
    chegada dos arquivos."""
    res = sb.table("tb_auditorias").select("arquivo_origem_score").eq("id_auditoria", id_auditoria).execute()
    atual = (res.data[0].get("arquivo_origem_score") if res.data else 0) or 0
    if score > atual:
        sb.table("tb_auditorias").update({
            "arquivo_origem": arquivo_origem,
            "arquivo_origem_score": score,
        }).eq("id_auditoria", id_auditoria).execute()


# ---------------------------------------------------------------------------
# tb_nao_conformidades
# ---------------------------------------------------------------------------
def find_nc(sb, id_auditoria, numero_item):
    res = sb.table("tb_nao_conformidades").select("*").eq("id_auditoria", id_auditoria).execute()
    alvo = _norm(numero_item)
    for row in res.data:
        if _norm(row["numero_item"]) == alvo:
            return row
    return None


def fetch_ncs_resumo(sb, id_auditoria):
    """numero_item + descricao de cada NC da auditoria — usado pra dar
    contexto à IA (no prompt principal ou no re-casamento) sem precisar
    buscar a linha inteira."""
    res = sb.table("tb_nao_conformidades").select("numero_item,descricao").eq("id_auditoria", id_auditoria).execute()
    return [row for row in res.data if row.get("numero_item")]


def upsert_nc(sb, id_auditoria, nc, arquivo_origem, link_arquivo=None):
    existente = find_nc(sb, id_auditoria, nc.get("numero_item"))
    if existente:
        sb.table("tb_nao_conformidades").update({
            "descricao": nc.get("descricao") or existente["descricao"],
            "categoria_nao_conformidade": nc.get("categoria_nao_conformidade") or existente.get("categoria_nao_conformidade"),
            "norma_referencia": nc.get("norma_referencia") or existente.get("norma_referencia"),
            "classificacao_gravidade": nc.get("classificacao_gravidade") or existente.get("classificacao_gravidade"),
            "acao_recomendada": nc.get("acao_recomendada") or existente.get("acao_recomendada"),
            "prazo_correcao": nc.get("prazo_correcao") or existente.get("prazo_correcao"),
        }).eq("id_nao_conformidade", existente["id_nao_conformidade"]).execute()
        id_nc = existente["id_nao_conformidade"]
    else:
        id_nc = gen_id("NC")
        sb.table("tb_nao_conformidades").insert({
            "id_nao_conformidade": id_nc,
            "id_auditoria": id_auditoria,
            "numero_item": nc.get("numero_item"),
            "descricao": nc.get("descricao"),
            "categoria_nao_conformidade": nc.get("categoria_nao_conformidade"),
            "norma_referencia": nc.get("norma_referencia"),
            "classificacao_gravidade": nc.get("classificacao_gravidade"),
            "acao_recomendada": nc.get("acao_recomendada"),
            "prazo_correcao": nc.get("prazo_correcao"),
            "status_atual": "pendente de resposta",
            "arquivo_origem": arquivo_origem,
        }).execute()

    insert_evidencias(sb, nc.get("evidencias"), arquivo_origem, id_nao_conformidade=id_nc, link_arquivo=link_arquivo)
    return id_nc


# ---------------------------------------------------------------------------
# tb_respostas
# ---------------------------------------------------------------------------
def insert_resposta(sb, id_nc, id_auditoria, tipo_registro, r, arquivo_origem, link_arquivo=None):
    id_resposta = gen_id("RESP")
    sb.table("tb_respostas").insert({
        "id_resposta": id_resposta,
        "id_nao_conformidade": id_nc,
        "id_auditoria": id_auditoria,
        "tipo_registro": tipo_registro,
        "resultado_final": r.get("resultado_final"),
        "decisao_anp": r.get("decisao_anp"),
        "texto_resposta": r.get("resumo"),
        "data_resposta": r.get("data_resposta"),
        "arquivo_origem": arquivo_origem,
    }).execute()
    # Evidência só é registrada pra resposta da OPERADORA — parecer da ANP não
    # gera linha em tb_evidencias (evidência é sempre algo apresentado pela
    # operadora em resposta, ou o próprio achado do DF).
    if tipo_registro == "resposta_operadora":
        insert_evidencias(sb, r.get("evidencias"), arquivo_origem, id_resposta=id_resposta, link_arquivo=link_arquivo)
    if r.get("resultado_final"):
        sb.table("tb_nao_conformidades").update(
            {"status_atual": r.get("resultado_final")}
        ).eq("id_nao_conformidade", id_nc).execute()


# ---------------------------------------------------------------------------
# tb_evidencias — 1 linha por evidência, ligada a uma não conformidade OU a
# uma resposta (nunca as duas), com origem (ANP ou operadora). Uma NC ou
# resposta pode ter várias evidências, por isso é lista, não campo único.
# ---------------------------------------------------------------------------
APRESENTADO_POR_VALIDOS = {"anp", "operadora"}

# Acha o número SEI citado dentro da própria descrição da evidência — o texto
# já vem nesse formato na maioria dos casos ("Carta DPBR-2024-11589
# (4529951)", "Parecer nº 197 (4837346)", "(SEI nº 5093911)"). Em vez de
# depender da IA emitir um campo JSON separado pra isso, extrai por regex:
# mais simples e funciona mesmo em evidências já extraídas antes dessa
# mudança. 6-8 dígitos evita casar ano ("2019"), percentual etc.
REGEX_SEI_REFERENCIADO = re.compile(r"\((?:sei\s*n?º?\s*)?(\d{6,8})\)", re.IGNORECASE)


def _extrai_arquivo_referenciado(descricao):
    m = REGEX_SEI_REFERENCIADO.search(descricao or "")
    return m.group(1) if m else None


def _normaliza_apresentado_por(valor):
    valor = (valor or "").strip().lower()
    return valor if valor in APRESENTADO_POR_VALIDOS else None


def insert_evidencias(sb, evidencias, arquivo_origem, id_nao_conformidade=None, id_resposta=None, link_arquivo=None):
    """Grava as evidências de UMA não conformidade OU UMA resposta (nunca as
    duas — respeita a mesma regra da constraint em tb_evidencias).

    "evidencias" é o array bruto que a IA devolveu — trata formatos
    imperfeitos (string solta em vez de objeto, valor de apresentado_por fora
    do esperado) sem quebrar, e evita duplicar a mesma evidência se o mesmo
    item for reprocessado ou citado de novo por outro documento.

    Pra cada evidência, tenta achar (via regex, ver `_extrai_arquivo_referenciado`)
    um número SEI citado na própria descrição — se achar E já existir o link
    daquele arquivo em `tb_arquivos_processados` (`fetch_link_arquivo`), o
    link da evidência aponta pro arquivo QUE TEM o detalhe, não pro
    documento atual (`link_arquivo`, usado só como fallback quando não há
    nada mais específico pra apontar).
    """
    if not evidencias:
        return
    if isinstance(evidencias, dict):
        evidencias = [evidencias]
    if not isinstance(evidencias, list):
        return

    query = sb.table("tb_evidencias").select("descricao")
    if id_nao_conformidade:
        query = query.eq("id_nao_conformidade", id_nao_conformidade)
    else:
        query = query.eq("id_resposta", id_resposta)
    ja_registradas = {_norm(row.get("descricao")) for row in query.execute().data}

    for ev in evidencias:
        if isinstance(ev, str):
            ev = {"descricao": ev, "apresentado_por": None}
        if not isinstance(ev, dict):
            continue
        descricao = (ev.get("descricao") or "").strip()
        if not descricao or _norm(descricao) in ja_registradas:
            continue

        arquivo_referenciado = _extrai_arquivo_referenciado(descricao)
        link_evidencia = link_arquivo
        if arquivo_referenciado:
            link_referenciado = fetch_link_arquivo(sb, arquivo_referenciado)
            if link_referenciado:
                link_evidencia = link_referenciado

        sb.table("tb_evidencias").insert({
            "id_evidencia": gen_id("EVID"),
            "id_nao_conformidade": id_nao_conformidade,
            "id_resposta": id_resposta,
            "apresentado_por": _normaliza_apresentado_por(ev.get("apresentado_por")),
            "descricao": descricao,
            "arquivo_referenciado": arquivo_referenciado,
            "link_evidencia": link_evidencia,
            "arquivo_origem": arquivo_origem,
        }).execute()
        ja_registradas.add(_norm(descricao))


def fetch_evidencias_por_anexo(sb, numero_sei):
    """Não conformidades que citaram este número SEI como fonte de mais
    detalhe (via `arquivo_referenciado`) — usado pra decidir, ANTES de
    descartar um arquivo tipo "Anexo", se vale a pena lê-lo."""
    if not numero_sei:
        return []
    res = (
        sb.table("tb_evidencias")
        .select("id_nao_conformidade,descricao")
        .eq("arquivo_referenciado", numero_sei)
        .not_.is_("id_nao_conformidade", "null")
        .execute()
    )
    return res.data


# ---------------------------------------------------------------------------
# Supabase Storage — evidências são salvas como o PDF de origem inteiro,
# disponibilizado por link público (não um recorte de página).
# ---------------------------------------------------------------------------
BUCKET_EVIDENCIAS = "evidencias"


def ensure_bucket(sb, nome=BUCKET_EVIDENCIAS):
    """Cria o bucket de evidências no Storage se ainda não existir.

    Idempotente — a API de storage não tem "create if not exists", então só
    ignora o erro de bucket já existente (qualquer outra falha real aparece
    de novo no upload logo em seguida, sem ficar mascarada aqui).
    """
    try:
        sb.storage.create_bucket(nome, options={"public": True})
    except Exception as e:
        if "already exists" not in str(e).lower() and "duplicate" not in str(e).lower():
            raise


def upload_evidencia_arquivo(sb, file_bytes, file_hash, nome_arquivo, bucket=BUCKET_EVIDENCIAS):
    """Sobe o PDF de origem pro Storage e devolve a URL pública.

    Usa o hash do arquivo como nome do objeto — o mesmo PDF nunca sobe duas
    vezes (reprocessamento, ou o mesmo arquivo citado por vários itens só
    reaproveita o link já existente via upsert).
    """
    ensure_bucket(sb, bucket)
    extensao = nome_arquivo.rsplit(".", 1)[-1] if "." in nome_arquivo else "pdf"
    caminho = f"{file_hash}.{extensao}"
    sb.storage.from_(bucket).upload(
        caminho, file_bytes,
        file_options={"content-type": "application/pdf", "upsert": "true"},
    )
    return sb.storage.from_(bucket).get_public_url(caminho)


# ---------------------------------------------------------------------------
# tb_processos_reprocessar — marca um processo inteiro para forçar releitura
# e reescrita mesmo sem o checkbox global de "forçar reprocessamento".
# ---------------------------------------------------------------------------
def marcar_processos_reprocessar(sb, numeros_processo_anp):
    for numero in numeros_processo_anp:
        numero = _norm_processo(numero)
        if numero:
            sb.table("tb_processos_reprocessar").upsert({"numero_processo_anp": numero}).execute()


def fetch_processos_reprocessar(sb):
    res = sb.table("tb_processos_reprocessar").select("*").execute()
    return {_norm_processo(row["numero_processo_anp"]) for row in res.data}


def desmarcar_processo_reprocessar(sb, numero_processo_anp):
    sb.table("tb_processos_reprocessar").delete().eq("numero_processo_anp", _norm_processo(numero_processo_anp)).execute()


# ---------------------------------------------------------------------------
# Leitura para exibição na interface
# ---------------------------------------------------------------------------
def fetch_all(sb, table):
    res = sb.table(table).select("*").order("criado_em", desc=True).execute()
    return res.data