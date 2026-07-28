"""Funções de acesso ao Fabric SQL Database (T-SQL, via pyodbc) e ao OneLake (Files do
Lakehouse) para as 7 tabelas do pipeline — porte do db.py original (Supabase/Postgres).

Trata o caso de upload em ordem aleatória: se um documento de resposta/parecer
chega antes da auditoria (ou de uma não conformidade específica) existir, o
resultado já extraído pela IA fica guardado em tb_arquivos_processados
(payload_json) e é retentado automaticamente sempre que uma auditoria nova é
gravada — sem precisar chamar a API de novo.

Cada função abre e fecha sua própria conexão (`_connect`) em vez de reaproveitar
uma conexão de longa duração — endpoints do Fabric SQL Database derrubam sessões
ociosas e o ODBC Driver 18 não faz pooling confiável aqui, e uma run do pipeline
pode passar minutos entre uma chamada de IA e a próxima escrita no banco.

`payload_json`/`itens_json` são colunas jsonb no Postgres original; aqui são
`nvarchar(max)` com `CHECK (ISJSON(...) = 1)` (ver schema.tsql.sql) — ao
contrário do client do Supabase, o pyodbc não desserializa JSON automaticamente,
então as funções que leem essas colunas fazem `json.loads` antes de devolver o
dict pro chamador, preservando o mesmo contrato de antes.
"""

import contextlib
import hashlib
import json
import os
import re
import struct
import uuid

import pyodbc
from azure.identity import DefaultAzureCredential

import pipeline

FABRIC_SQL_SERVER = os.environ.get("FABRIC_SQL_SERVER")
FABRIC_SQL_DATABASE = os.environ.get("FABRIC_SQL_DATABASE")

_SQL_COPT_SS_ACCESS_TOKEN = 1256
_credential = DefaultAzureCredential()


@contextlib.contextmanager
def _connect():
    token = _credential.get_token("https://database.windows.net/.default").token.encode("utf-16-le")
    token_struct = struct.pack(f"<I{len(token)}s", len(token), token)
    conn_str = (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server={FABRIC_SQL_SERVER};Database={FABRIC_SQL_DATABASE};"
        "Encrypt=yes;TrustServerCertificate=no;"
    )
    conn = pyodbc.connect(conn_str, attrs_before={_SQL_COPT_SS_ACCESS_TOKEN: token_struct})
    try:
        yield conn
    finally:
        conn.close()


def _row_to_dict(cursor, row):
    return dict(zip((c[0] for c in cursor.description), row))


def _fetch_all(cursor):
    return [_row_to_dict(cursor, row) for row in cursor.fetchall()]


def _parse_json_field(row, field):
    if row.get(field):
        row[field] = json.loads(row[field])
    return row


def _to_json(value):
    return json.dumps(value, ensure_ascii=False) if value is not None else None


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


def _upsert(table, pk_col, pk_val, values):
    """INSERT ou UPDATE por chave primária. As chaves de `values` vêm sempre de
    dicts literais escritos neste módulo (nunca de JSON externo/não confiável),
    então interpolar como nome de coluna é seguro — mesmo raciocínio da allowlist
    CAMPOS_AUDITORIA abaixo."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT 1 FROM dbo.{table} WHERE {pk_col} = ?", pk_val)
        exists = cur.fetchone() is not None
        if exists:
            if values:
                set_clause = ", ".join(f"{c} = ?" for c in values)
                cur.execute(f"UPDATE dbo.{table} SET {set_clause} WHERE {pk_col} = ?", [*values.values(), pk_val])
        else:
            all_cols = [pk_col, *values.keys()]
            placeholders = ", ".join("?" for _ in all_cols)
            cur.execute(
                f"INSERT INTO dbo.{table} ({', '.join(all_cols)}) VALUES ({placeholders})",
                [pk_val, *values.values()],
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Controle de duplicidade / fila de pendências
# ---------------------------------------------------------------------------
def already_processed(file_hash):
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM dbo.tb_arquivos_processados WHERE hash_arquivo = ?", file_hash)
        row = cur.fetchone()
        if not row:
            return None
        return _parse_json_field(_row_to_dict(cur, row), "payload_json")


def upsert_arquivo_status(file_hash, nome_arquivo, tipo_documento, confianca, status, motivo=None, payload_json=None,
                           numero_sei=None, numero_processo_anp=None, numero_processo_divergente=None, link_arquivo=None,
                           data_emissao_documento=None):
    _upsert("tb_arquivos_processados", "hash_arquivo", file_hash, {
        "nome_arquivo": nome_arquivo,
        "tipo_documento": tipo_documento,
        "confianca": confianca,
        "status": status,
        "motivo": motivo,
        "payload_json": _to_json(payload_json),
        "numero_sei": numero_sei,
        "numero_processo_anp": numero_processo_anp,
        "numero_processo_divergente": numero_processo_divergente,
        "link_arquivo": link_arquivo,
        "data_emissao_documento": data_emissao_documento,
    })


def fetch_link_arquivo(numero_sei):
    """Link (caminho OneLake) de um arquivo já processado, buscado pelo número SEI
    (nome do arquivo sem extensão) — usado pra resolver o link de uma
    evidência pro arquivo que realmente tem o detalhe, não pro documento
    principal que só cita esse número."""
    if not numero_sei:
        return None
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT TOP 1 link_arquivo FROM dbo.tb_arquivos_processados "
            "WHERE numero_sei = ? AND link_arquivo IS NOT NULL",
            numero_sei,
        )
        row = cur.fetchone()
        return row[0] if row else None


def fetch_pendentes():
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM dbo.tb_arquivos_processados WHERE status IN ('pendente_vinculo', 'parcial')")
        return [_parse_json_field(row, "payload_json") for row in _fetch_all(cur)]


# ---------------------------------------------------------------------------
# tb_lotes_batch — processamento em lote via Batch API da Anthropic
# ---------------------------------------------------------------------------
def upsert_lote_batch(id_lote, status, total_itens, itens_json, nome_arquivo_zip=None):
    values = {"status": status, "total_itens": total_itens, "itens_json": _to_json(itens_json)}
    # só inclui se informado — evita apagar o nome já gravado quando esta
    # função é chamada de novo só pra atualizar status (process_pending_batches)
    if nome_arquivo_zip is not None:
        values["nome_arquivo_zip"] = nome_arquivo_zip
    _upsert("tb_lotes_batch", "id_lote", id_lote, values)


def fetch_lotes_batch(status_excluir=None):
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM dbo.tb_lotes_batch ORDER BY criado_em DESC")
        dados = [_parse_json_field(row, "itens_json") for row in _fetch_all(cur)]
    if status_excluir:
        dados = [l for l in dados if l["status"] != status_excluir]
    return dados


# ---------------------------------------------------------------------------
# tb_auditorias
# ---------------------------------------------------------------------------
def find_auditoria(numero_processo_anp=None, numero_relatorio=None):
    if numero_processo_anp:
        alvo = _norm_processo(numero_processo_anp)
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM dbo.tb_auditorias")
            for row in _fetch_all(cur):
                if _norm_processo(row.get("numero_processo_anp")) == alvo:
                    return row
    if numero_relatorio:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM dbo.tb_auditorias WHERE numero_relatorio LIKE ?", f"%{numero_relatorio}%")
            rows = _fetch_all(cur)
            if rows:
                return rows[0]
    return None


# Colunas de tb_auditorias que a IA pode preencher (exclui id_auditoria,
# arquivo_origem, criado_em, atualizado_em, que são geridas aqui). "dados"
# vem de um objeto JSON que a própria IA gerou e é espalhado (**dados) direto
# no payload — sem esse filtro, qualquer chave inesperada que o modelo
# inventasse (nome de campo diferente, campo extra) ia direto pro SQL e
# quebrava com "invalid column name", derrubando o processamento inteiro em
# vez de só marcar aquele arquivo como erro.
CAMPOS_AUDITORIA = {
    "numero_processo_anp", "numero_relatorio", "codigo_auditoria_anp", "ordem_servico",
    "operadora", "cnpj_operadora", "unidade_instalacao", "tipo_auditoria",
    "sumario_auditoria", "acao_demandada", "data_auditoria_inicio", "data_auditoria_fim",
    "data_emissao_relatorio", "auditor_responsavel", "auditores_equipe", "status_auditoria",
    "resultado_final",
}


# ---------------------------------------------------------------------------
# "Mais recente vence" — como a data de emissão só é conhecida DEPOIS que a
# IA lê o conteúdo do documento (não dá pra ordenar a fila de processamento
# antes disso, e a Batch API da Anthropic nem devolve resultados na ordem de
# envio), a única forma confiável de garantir que o dado mais atual prevaleça
# é travar a GRAVAÇÃO: só sobrescreve um campo já preenchido se o documento
# novo tiver data de emissão igual ou mais recente que a já registrada.
# ---------------------------------------------------------------------------
def _atual_e_mais_recente(nova_data, atual_data):
    """True => o dado JÁ GRAVADO é igual ou mais recente que o novo, então
    NÃO deve ser sobrescrito. Data desconhecida nunca vence uma data
    conhecida; entre duas datas desconhecidas, mantém o comportamento antigo
    (o processamento mais recente vence, já que não há como comparar)."""
    if nova_data and atual_data:
        return atual_data > nova_data
    if nova_data and not atual_data:
        return False
    if not nova_data and atual_data:
        return True
    return False


def upsert_auditoria(dados, arquivo_origem, data_emissao=None):
    dados = {k: v for k, v in dados.items() if k in CAMPOS_AUDITORIA}
    existente = find_auditoria(dados.get("numero_processo_anp"), dados.get("numero_relatorio"))
    if existente:
        id_auditoria = existente["id_auditoria"]
        if not _atual_e_mais_recente(data_emissao, existente.get("data_emissao_origem")):
            payload = {k: v for k, v in dados.items() if v is not None}
            if data_emissao:
                payload["data_emissao_origem"] = data_emissao
            if payload:
                _upsert("tb_auditorias", "id_auditoria", id_auditoria, payload)
    else:
        id_auditoria = gen_id("AUD")
        payload = {"arquivo_origem": arquivo_origem, "data_emissao_origem": data_emissao, **dados}
        _upsert("tb_auditorias", "id_auditoria", id_auditoria, payload)
    return id_auditoria


def atualizar_arquivo_origem_df(id_auditoria, arquivo_origem, score):
    """Só troca arquivo_origem (+ arquivo_origem_score) da auditoria se o novo
    score for MAIOR que o já salvo — garante que, quando há mais de um DF no
    mesmo processo, o mais informativo (mais não conformidades e mais campos
    de auditoria preenchidos) fique como referência, não importa a ordem de
    chegada dos arquivos."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT arquivo_origem_score FROM dbo.tb_auditorias WHERE id_auditoria = ?", id_auditoria)
        row = cur.fetchone()
        atual = (row[0] if row else 0) or 0
        if score > atual:
            cur.execute(
                "UPDATE dbo.tb_auditorias SET arquivo_origem = ?, arquivo_origem_score = ? WHERE id_auditoria = ?",
                arquivo_origem, score, id_auditoria,
            )
            conn.commit()


# ---------------------------------------------------------------------------
# tb_nao_conformidades
# ---------------------------------------------------------------------------
def find_nc(id_auditoria, numero_item):
    alvo = _norm(numero_item)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM dbo.tb_nao_conformidades WHERE id_auditoria = ?", id_auditoria)
        for row in _fetch_all(cur):
            if _norm(row["numero_item"]) == alvo:
                return row
    return None


def fetch_ncs_resumo(id_auditoria):
    """numero_item + descricao de cada NC da auditoria — usado pra dar
    contexto à IA (no prompt principal ou no re-casamento) sem precisar
    buscar a linha inteira."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT numero_item, descricao FROM dbo.tb_nao_conformidades WHERE id_auditoria = ?", id_auditoria)
        return [row for row in _fetch_all(cur) if row.get("numero_item")]


def upsert_nc(id_auditoria, nc, arquivo_origem, link_arquivo=None, data_emissao=None):
    existente = find_nc(id_auditoria, nc.get("numero_item"))
    if existente:
        id_nc = existente["id_nao_conformidade"]
        if not _atual_e_mais_recente(data_emissao, existente.get("data_emissao_origem")):
            update_payload = {
                "descricao": nc.get("descricao") or existente["descricao"],
                "categoria_nao_conformidade": nc.get("categoria_nao_conformidade") or existente.get("categoria_nao_conformidade"),
                "norma_referencia": nc.get("norma_referencia") or existente.get("norma_referencia"),
                "classificacao_gravidade": nc.get("classificacao_gravidade") or existente.get("classificacao_gravidade"),
                "acao_recomendada": nc.get("acao_recomendada") or existente.get("acao_recomendada"),
                "prazo_correcao": nc.get("prazo_correcao") or existente.get("prazo_correcao"),
            }
            if data_emissao:
                update_payload["data_emissao_origem"] = data_emissao
            _upsert("tb_nao_conformidades", "id_nao_conformidade", id_nc, update_payload)
    else:
        id_nc = gen_id("NC")
        _upsert("tb_nao_conformidades", "id_nao_conformidade", id_nc, {
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
            "data_emissao_origem": data_emissao,
        })

    insert_evidencias(nc.get("evidencias"), arquivo_origem, id_nao_conformidade=id_nc, link_arquivo=link_arquivo)
    return id_nc


# ---------------------------------------------------------------------------
# tb_respostas
# ---------------------------------------------------------------------------
def insert_resposta(id_nc, id_auditoria, tipo_registro, r, arquivo_origem, link_arquivo=None, data_emissao=None):
    id_resposta = gen_id("RESP")
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO dbo.tb_respostas "
            "(id_resposta, id_nao_conformidade, id_auditoria, tipo_registro, resultado_final, decisao_anp, "
            "texto_resposta, data_resposta, arquivo_origem) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            id_resposta, id_nc, id_auditoria, tipo_registro,
            r.get("resultado_final"), r.get("decisao_anp"), r.get("resumo"), r.get("data_resposta"), arquivo_origem,
        )
        conn.commit()

    # Evidência só é registrada pra resposta da OPERADORA — parecer da ANP não
    # gera linha em tb_evidencias (evidência é sempre algo apresentado pela
    # operadora em resposta, ou o próprio achado do DF).
    if tipo_registro == "resposta_operadora":
        insert_evidencias(r.get("evidencias"), arquivo_origem, id_resposta=id_resposta, link_arquivo=link_arquivo)

    if r.get("resultado_final"):
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT status_data_origem FROM dbo.tb_nao_conformidades WHERE id_nao_conformidade = ?", id_nc)
            row = cur.fetchone()
            status_data_atual = row[0] if row else None
            if not _atual_e_mais_recente(data_emissao, status_data_atual):
                cur.execute(
                    "UPDATE dbo.tb_nao_conformidades SET status_atual = ?, status_data_origem = ? WHERE id_nao_conformidade = ?",
                    r.get("resultado_final"), data_emissao, id_nc,
                )
                conn.commit()


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


def _resolver_html_documento(cache, link_arquivo):
    """Gera (uma vez só, cacheado em `cache`) o HTML com âncora por parágrafo
    do documento publicado em `link_arquivo` (caminho OneLake), lendo o
    arquivo direto do Lakehouse montado (`Files/evidencias/...`) — o notebook
    e os Files estão no mesmo filesystem, então não precisa mais rebaixar o
    PDF por HTTP como no Supabase Storage original. Devolve (html_url, chunks)
    ou None se não foi possível ler/extrair texto do PDF."""
    if link_arquivo in cache:
        return cache[link_arquivo]
    resolved = None
    if link_arquivo and os.path.exists(link_arquivo):
        with open(link_arquivo, "rb") as f:
            pdf_bytes = f.read()
        paginas = pipeline.extract_pages(pdf_bytes)
        if any(p.strip() for p in paginas):
            html_str, chunks = pipeline.gerar_html_documento(paginas)
            html_url = upload_html_documento(html_str)
            resolved = (html_url, chunks)
    cache[link_arquivo] = resolved
    return resolved


def insert_evidencias(evidencias, arquivo_origem, id_nao_conformidade=None, id_resposta=None, link_arquivo=None):
    """Grava as evidências de UMA não conformidade OU UMA resposta (nunca as
    duas — respeita a mesma regra da constraint em tb_evidencias).

    "evidencias" é o array bruto que a IA devolveu — trata formatos
    imperfeitos (string solta em vez de objeto, valor de apresentado_por fora
    do esperado) sem quebrar, e evita duplicar a mesma evidência se o mesmo
    item for reprocessado ou citado de novo por outro documento.

    Quando a evidência vem com `trecho_original`/`pagina_aproximada`, o link
    aponta pro parágrafo exato (âncora `#id`) do HTML gerado do documento
    ATUAL (`link_arquivo` — é sempre o doc cujo texto gerou esse trecho, seja
    o DF ou um anexo; o HTML é gerado/subido uma vez só por documento, mesmo
    que várias evidências desta chamada apontem pra ele). Sem trecho
    localizável, cai no comportamento antigo: tenta achar (via regex, ver
    `_extrai_arquivo_referenciado`) um número SEI citado na própria descrição
    — se achar E já existir o link daquele arquivo em `tb_arquivos_processados`
    (`fetch_link_arquivo`), o link aponta pro arquivo QUE TEM o detalhe, senão
    cai em `link_arquivo` (o PDF inteiro).
    """
    if not evidencias:
        return
    if isinstance(evidencias, dict):
        evidencias = [evidencias]
    if not isinstance(evidencias, list):
        return

    with _connect() as conn, conn.cursor() as cur:
        if id_nao_conformidade:
            cur.execute("SELECT descricao FROM dbo.tb_evidencias WHERE id_nao_conformidade = ?", id_nao_conformidade)
        else:
            cur.execute("SELECT descricao FROM dbo.tb_evidencias WHERE id_resposta = ?", id_resposta)
        ja_registradas = {_norm(row[0]) for row in cur.fetchall()}

    html_cache = {}

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
            link_referenciado = fetch_link_arquivo(arquivo_referenciado)
            if link_referenciado:
                link_evidencia = link_referenciado

        pagina_evidencia = None
        tipo_link_evidencia = "documento_completo"
        trecho = (ev.get("trecho_original") or "").strip() or None
        try:
            pagina_aproximada = int(ev.get("pagina_aproximada"))
        except (TypeError, ValueError):
            pagina_aproximada = None
        if (trecho or pagina_aproximada) and link_arquivo:
            resolved = _resolver_html_documento(html_cache, link_arquivo)
            if resolved:
                html_url, chunks = resolved
                ancora = pipeline.localizar_ancora(chunks, trecho, pagina_aproximada)
                if ancora:
                    _id, pagina_evidencia, tipo_link_evidencia = ancora
                    link_evidencia = f"{html_url}#{_id}"

        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO dbo.tb_evidencias "
                "(id_evidencia, id_nao_conformidade, id_resposta, apresentado_por, descricao, sistema_evidencia, "
                "equipamento_evidencia, categoria_evidencia, pagina_evidencia, tipo_link_evidencia, "
                "arquivo_referenciado, link_evidencia, arquivo_origem) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                gen_id("EVID"), id_nao_conformidade, id_resposta, _normaliza_apresentado_por(ev.get("apresentado_por")),
                descricao, ev.get("sistema") or None, ev.get("equipamento") or None, ev.get("categoria") or None,
                pagina_evidencia, tipo_link_evidencia, arquivo_referenciado, link_evidencia, arquivo_origem,
            )
            conn.commit()
        ja_registradas.add(_norm(descricao))


def fetch_evidencias_por_anexo(numero_sei):
    """Não conformidades que citaram este número SEI como fonte de mais
    detalhe (via `arquivo_referenciado`) — usado pra decidir, ANTES de
    descartar um arquivo tipo "Anexo", se vale a pena lê-lo."""
    if not numero_sei:
        return []
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id_nao_conformidade, descricao FROM dbo.tb_evidencias "
            "WHERE arquivo_referenciado = ? AND id_nao_conformidade IS NOT NULL",
            numero_sei,
        )
        return _fetch_all(cur)


# ---------------------------------------------------------------------------
# OneLake (Files do Lakehouse) — evidências são salvas como o PDF de origem
# inteiro, disponibilizado por caminho no Lakehouse montado (não mais um link
# público como no Supabase Storage — só quem tem acesso ao workspace Fabric
# abre o arquivo, o que é uma melhoria de segurança sobre o bucket público do
# protótipo anterior).
# ---------------------------------------------------------------------------
LAKEHOUSE_FILES_ROOT = os.environ.get("LAKEHOUSE_FILES_ROOT", "/lakehouse/default/Files")
DIR_EVIDENCIAS = "evidencias"
DIR_EVIDENCIAS_HTML = "evidencias-html"


def upload_evidencia_arquivo(file_bytes, file_hash, nome_arquivo):
    """Sobe o PDF de origem pro Lakehouse e devolve o caminho.

    Usa o hash do arquivo como nome do objeto — o mesmo PDF nunca sobe duas
    vezes (reprocessamento, ou o mesmo arquivo citado por vários itens só
    reaproveita o caminho já existente).
    """
    extensao = nome_arquivo.rsplit(".", 1)[-1] if "." in nome_arquivo else "pdf"
    pasta = os.path.join(LAKEHOUSE_FILES_ROOT, DIR_EVIDENCIAS)
    os.makedirs(pasta, exist_ok=True)
    caminho = os.path.join(pasta, f"{file_hash}.{extensao}")
    if not os.path.exists(caminho):
        with open(caminho, "wb") as f:
            f.write(file_bytes)
    return caminho


def upload_html_documento(html_str):
    """Sobe o HTML gerado do documento-fonte e devolve o caminho.

    Nome do objeto = hash do próprio conteúdo — o mesmo PDF sempre extrai o
    mesmo texto e gera o mesmo HTML, então sobe uma vez só mesmo que várias
    evidências (de documentos diferentes) apontem pro mesmo anexo/DF.
    """
    pasta = os.path.join(LAKEHOUSE_FILES_ROOT, DIR_EVIDENCIAS_HTML)
    os.makedirs(pasta, exist_ok=True)
    caminho = os.path.join(pasta, f"{hashlib.sha256(html_str.encode('utf-8')).hexdigest()}.html")
    if not os.path.exists(caminho):
        with open(caminho, "w", encoding="utf-8") as f:
            f.write(html_str)
    return caminho


# ---------------------------------------------------------------------------
# tb_processos_reprocessar — marca um processo inteiro para forçar releitura
# e reescrita na próxima vez que seus arquivos passarem pela varredura da
# pasta de entrada (só funciona se o(s) arquivo(s) do processo ainda
# estiverem lá — ver runbook no README).
# ---------------------------------------------------------------------------
def marcar_processos_reprocessar(numeros_processo_anp):
    for numero in numeros_processo_anp:
        numero = _norm_processo(numero)
        if numero:
            _upsert("tb_processos_reprocessar", "numero_processo_anp", numero, {})


def fetch_processos_reprocessar():
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT numero_processo_anp FROM dbo.tb_processos_reprocessar")
        return {_norm_processo(row[0]) for row in cur.fetchall()}


def desmarcar_processo_reprocessar(numero_processo_anp):
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM dbo.tb_processos_reprocessar WHERE numero_processo_anp = ?", _norm_processo(numero_processo_anp))
        conn.commit()
