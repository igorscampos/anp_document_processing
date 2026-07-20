"""Funções de acesso ao Supabase (Postgres) para as 4 tabelas do pipeline.

Trata o caso de upload em ordem aleatória: se um documento de resposta/parecer
chega antes da auditoria (ou de uma não conformidade específica) existir, o
resultado já extraído pela IA fica guardado em tb_arquivos_processados
(payload_json) e é retentado automaticamente sempre que uma auditoria nova é
gravada — sem precisar chamar a API de novo.
"""

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


def upsert_arquivo_status(sb, file_hash, nome_arquivo, tipo_documento, confianca, status, motivo=None, payload_json=None):
    sb.table("tb_arquivos_processados").upsert({
        "hash_arquivo": file_hash,
        "nome_arquivo": nome_arquivo,
        "tipo_documento": tipo_documento,
        "confianca": confianca,
        "status": status,
        "motivo": motivo,
        "payload_json": payload_json,
    }).execute()


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
def upsert_lote_batch(sb, id_lote, status, total_itens, itens_json):
    sb.table("tb_lotes_batch").upsert({
        "id_lote": id_lote,
        "status": status,
        "total_itens": total_itens,
        "itens_json": itens_json,
    }).execute()


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
    "data_emissao_relatorio", "auditor_responsavel", "status_auditoria", "resultado_final",
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


def upsert_nc(sb, id_auditoria, nc, arquivo_origem):
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
        return existente["id_nao_conformidade"]
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
    return id_nc


# ---------------------------------------------------------------------------
# tb_respostas
# ---------------------------------------------------------------------------
def insert_resposta(sb, id_nc, id_auditoria, tipo_registro, r, arquivo_origem):
    sb.table("tb_respostas").insert({
        "id_resposta": gen_id("RESP"),
        "id_nao_conformidade": id_nc,
        "id_auditoria": id_auditoria,
        "tipo_registro": tipo_registro,
        "resultado_final": r.get("resultado_final"),
        "decisao_anp": r.get("decisao_anp"),
        "texto_resposta": r.get("resumo"),
        "arquivo_origem": arquivo_origem,
    }).execute()
    if r.get("resultado_final"):
        sb.table("tb_nao_conformidades").update(
            {"status_atual": r.get("resultado_final")}
        ).eq("id_nao_conformidade", id_nc).execute()


# ---------------------------------------------------------------------------
# Leitura para exibição na interface
# ---------------------------------------------------------------------------
def fetch_all(sb, table):
    res = sb.table(table).select("*").order("criado_em", desc=True).execute()
    return res.data