"""Lógica de extração de texto, catálogo de tipos de documento (CSV do SEI) e
prompt de classificação — compartilhada entre o app Streamlit (streamlit_app.py)
e scripts de teste/linha de comando.

O CSV do SEI (nº processo sei, documento, tipo_documento, data_captura, status)
é a fonte de verdade obrigatória do pipeline: só processamos documento que
esteja mapeado nele, e o número de processo usado pra vincular tudo vem dele
(não de regex no texto nem de suposição da IA). Usamos essa informação para
três coisas, ANTES de chamar a IA:

1. Descartar de cara arquivos com erro de captura, cujo tipo nunca carrega
   conteúdo de auditoria/não-conformidade (e-mail, recibo de protocolo, anexo
   solto, despacho de trâmite etc.), OU que simplesmente não estão mapeados
   no CSV — evita gastar tokens à toa e evita processar algo fora do escopo
   conhecido do processo.
2. Marcar com certeza os documentos que SÃO o Documento de Fiscalização (ou
   peça equivalente da família de fiscalização) como "auditoria_oficial",
   em vez de confiar na IA para adivinhar isso — essa era a principal fonte
   de classificação errada.
3. Fornecer o número de processo ANP autoritativo (coluna "nº processo sei")
   — qualquer número diferente encontrado no texto do documento é só
   registrado como divergência, nunca usado pra vínculo.
"""

import csv
import io
import re

import pdfplumber

# ---------------------------------------------------------------------------
# Número de processo ANP — extração determinística por regex
# ---------------------------------------------------------------------------
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


# Pastas de processo em lotes ZIP costumam vir nomeadas como o próprio número
# do processo, só que com "_" no lugar da "/" (ex: "48610.229661_2024-49") —
# artefato comum de export do SEI, já que "/" não é válido em nome de pasta.
REGEX_PASTA_PROCESSO = re.compile(r"^(\d{4,5}\.\d{6})_(\d{4}-\d{2})$")


def extract_processo_from_foldername(nome_pasta):
    """Fallback de número de processo quando não há match do regex no texto.

    Usado no processamento em lote: se o PDF não tiver o rodapé padrão do SEI
    (ex: um Ofício mais curto), o nome da pasta ainda entrega o processo.
    """
    m = REGEX_PASTA_PROCESSO.match((nome_pasta or "").strip())
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return None


# ---------------------------------------------------------------------------
# Extração de texto
# ---------------------------------------------------------------------------
def extract_text(file_bytes, max_chars):
    parts = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n".join(parts)[:max_chars]


# ---------------------------------------------------------------------------
# Catálogo de tipos de documento (CSV exportado do SEI)
# ---------------------------------------------------------------------------

# Tipos que são só metadado/roteamento administrativo do SEI, sem conteúdo de
# auditoria ou não-conformidade extraível — descartados sem chamar a IA.
# "Anexo" entra aqui porque hoje a própria IA já classifica esse tipo como
# "evidencia_anexa" e manda para revisão manual sem extrair nada automaticamente
# — ou seja, o texto era lido e mandado para o modelo sem nunca virar dado
# aproveitado. Melhor nem gastar o token.
TIPOS_DESCARTAR = {
    "e-mail",
    "recibo eletrônico de protocolo",
    "anexo",
    "publicação",
    "despacho de encaminhamento",
    "despacho para publicação",
    "despacho de proposta para deliberação da diretoria",
    "termo de encerramento de trâmite físico",
    "termo de arquivamento de processo eletrônico",
    "ficha para autuação de processo e distribuição",
    "certidão de distribuição",
    "certidão de redistribuição",
    "procuração",
    "boleto",
    "guia",
    "comprovante",
    "aviso",
    "pauta",
    "confirmação",
    "processo",
    "lista",
    "planilha",
    "ficha",
    "folha",
    "protocolo",
    "certidão",
    "certificado",
    "prontuário",
    "cota",
}

# Tipos que são o Documento de Fiscalização ou peças oficiais equivalentes da
# mesma família (ordem de serviço, plano e relatório de auditoria) — tratados
# como "auditoria_oficial" direto pelo tipo do SEI, sem depender da IA acertar
# a classificação.
TIPOS_PRIORITARIOS = {
    "documento de fiscalização (df)",
    "documento de fiscalização",
    "relatório de auditoria de segurança",
    "relatório de auditoria",
    "fiscalização: segurança operacional",
    "ordem de serviço de fiscalização",
    "plano de auditoria de segurança",
    "plano de auditoria",
}

# Subconjunto de TIPOS_PRIORITARIOS que é o Documento de Fiscalização em si
# (não a Ordem de Serviço, o Plano ou o Relatório, que também são
# "auditoria_oficial" mas não devem gerar não conformidade — só o DF é a
# fonte de não conformidades).
TIPOS_DF = {
    "documento de fiscalização (df)",
    "documento de fiscalização",
}


def _norm_tipo(tipo):
    return (tipo or "").strip().lower()


def is_tipo_df(tipo_sei):
    """True só para o Documento de Fiscalização em si — não pra Ordem de
    Serviço, Plano ou Relatório, que são "auditoria_oficial" mas não devem
    gerar não conformidade (só o DF é a fonte de não conformidades)."""
    return _norm_tipo(tipo_sei) in TIPOS_DF


def is_tipo_anexo(tipo_sei):
    """True só para o tipo "Anexo" — usado pra decidir se vale a pena checar,
    ANTES de descartar, se algum outro documento já citou este anexo
    especificamente como fonte de mais detalhe de evidência. Só se aplica a
    este tipo: E-mail, Recibo e os demais tipos de TIPOS_DESCARTAR nunca
    passam por essa checagem, mesmo que algum texto cite o número deles."""
    return _norm_tipo(tipo_sei) == "anexo"


# O CSV do SEI vem com cabeçalhos em português ("nº processo sei", "documento",
# "tipo_documento"), mas outras fontes de export (ex: planilha baixada do
# Excel) usam cabeçalhos em inglês ("process_number", "document_name",
# "document_type") para o mesmo conteúdo. Sem isso, o código procurava só o
# nome em português, não achava a coluna, e carregava um catálogo VAZIO sem
# avisar — a UI só dizia "catálogo não enviado" mesmo com o arquivo certo.
_ALIAS_COLUNAS = {
    "documento": ("documento", "document_name", "document", "nome_documento", "numero_documento"),
    "processo": (
        "nº processo sei", "n° processo sei", "no processo sei", "numero processo sei",
        "numero_processo_sei", "process_number", "processo", "numero_processo",
    ),
    "tipo_documento": ("tipo_documento", "tipo documento", "document_type"),
    "status": ("status", "situacao", "situação"),
}


def _norm_header(h):
    return re.sub(r"\s+", " ", (h or "").strip().lower())


def _resolver_colunas(fieldnames):
    """Acha, pra cada campo esperado, qual coluna do CSV corresponde — aceita
    tanto os cabeçalhos do SEI em português quanto variantes em inglês."""
    disponiveis = {_norm_header(fn): fn for fn in (fieldnames or [])}
    return {
        campo: next((disponiveis[_norm_header(c)] for c in candidatos if _norm_header(c) in disponiveis), None)
        for campo, candidatos in _ALIAS_COLUNAS.items()
    }


def load_catalog(csv_source):
    """Lê o CSV nº processo sei / documento / tipo_documento / status.

    Aceita um caminho de arquivo (str) ou um objeto file-like já aberto em
    modo texto (ex: io.StringIO, ou o retorno de um file_uploader do Streamlit).
    Reconhece tanto os cabeçalhos originais do SEI quanto variantes em inglês
    (ex: "process_number", "document_name", "document_type").

    Retorna dict {numero_documento(str): {"processo", "tipo_documento", "status"}}.
    """
    catalogo = {}

    def _parse(f):
        reader = csv.DictReader(f)
        colunas = _resolver_colunas(reader.fieldnames)
        if not colunas["documento"] or not colunas["processo"]:
            encontradas = ", ".join(reader.fieldnames or []) or "(nenhuma)"
            raise ValueError(
                "Não reconheci as colunas de documento/processo neste CSV. "
                f"Colunas encontradas: {encontradas}. "
                "Renomeie para 'documento' e 'nº processo sei' (ou 'document_name' / 'process_number')."
            )
        for row in reader:
            doc_id = (row.get(colunas["documento"]) or "").strip()
            if not doc_id:
                continue
            catalogo[doc_id] = {
                "processo": (row.get(colunas["processo"]) or "").strip(),
                "tipo_documento": (row.get(colunas["tipo_documento"]) or "").strip() if colunas["tipo_documento"] else "",
                "status": (row.get(colunas["status"]) or "").strip() if colunas["status"] else "",
            }

    if isinstance(csv_source, str):
        with open(csv_source, newline="", encoding="utf-8-sig") as f:
            _parse(f)
    else:
        _parse(csv_source)
    return catalogo


def doc_id_from_filename(filename):
    """'4516505.pdf' -> '4516505' (número do documento no SEI == nome do arquivo)."""
    base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return base.rsplit(".", 1)[0].strip()


def classify_catalog_entry(entry):
    """Decide o que fazer com o arquivo ANTES de gastar tokens.

    Retorna "descartar" | "prioritario" | "processar".
    - "descartar": não chama a IA — arquivo é ruído administrativo, falhou a
      captura no SEI, OU não está mapeado no catálogo (o CSV é a fonte de
      verdade obrigatória: se o documento não está nele, não processamos).
    - "prioritario": chama a IA só para extrair campos, mas o tipo_documento
      final é forçado para "auditoria_oficial" (não depende da IA classificar).
    - "processar": segue o fluxo normal, IA decide o tipo_documento.
    """
    if not entry:
        return "descartar"
    if entry["status"] and entry["status"].strip().lower() != "sucesso":
        return "descartar"
    tipo = _norm_tipo(entry["tipo_documento"])
    if tipo in TIPOS_DESCARTAR:
        return "descartar"
    if tipo in TIPOS_PRIORITARIOS:
        return "prioritario"
    return "processar"


# ---------------------------------------------------------------------------
# Prompt de classificação/extração
# ---------------------------------------------------------------------------
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
{dica_tipo_sei}
Preencha "auditoria" (objeto com codigo_auditoria_anp, ordem_servico, operadora, cnpj_operadora,
unidade_instalacao, tipo_auditoria, sumario_auditoria, acao_demandada, data_auditoria_inicio,
data_auditoria_fim, data_emissao_relatorio, auditor_responsavel, auditores_equipe, status_auditoria,
resultado_final) SOMENTE se tipo_documento for auditoria_oficial. O anexo técnico de uma auditoria
também conta como auditoria_oficial.

Preencha "nao_conformidades" (array de {{numero_item, descricao, categoria_nao_conformidade,
norma_referencia, classificacao_gravidade, acao_recomendada, prazo_correcao, evidencias}}) SOMENTE
se tipo_documento for auditoria_oficial E o próprio documento for o Documento de Fiscalização (DF)
em si. Ordem de Serviço, Plano de Auditoria e Relatório de Auditoria também são auditoria_oficial,
mas NÃO são o DF — preencha "auditoria" normalmente para eles, e deixe "nao_conformidades" como
array vazio [], mesmo que o texto mencione problemas encontrados. Se houver dúvida sobre qual
documento é o DF, use a dica do sistema de origem (SEI) abaixo, quando presente.

Detalhe de cada campo novo de "auditoria":
- codigo_auditoria_anp: identificador interno da própria ANP para a auditoria, geralmente no
  formato "NNN_SSO_AAAA" (ex: "027_SSO_2025"), citado como "Auditoria 027_SSO_2025" no texto.
  Diferente do número do processo administrativo e do número do relatório/DF.
- ordem_servico: número da Ordem de Serviço, geralmente no cabeçalho do documento (ex: "OS SSO_002725").
- sumario_auditoria: resumo em até 40 palavras do que a auditoria constatou/tratou.
- acao_demandada: a medida cautelar ou ação exigida pela ANP como resultado da auditoria (ex:
  "Interdição da operação com hidrocarbonetos até saneamento das notificações"), em até 30 palavras.
- auditor_responsavel: o nome do auditor LÍDER/responsável pela auditoria.
- auditores_equipe: nomes dos DEMAIS integrantes da equipe de auditoria (além do auditor_responsavel),
  concatenados separados por "; " (ex: "João Silva; Maria Santos"). null se não houver outros nomes
  citados ou se só um auditor for mencionado.
- resultado_final: a conclusão/status final da auditoria SE o documento deixar isso claro (ex:
  "interditada", "liberada para operação"); use null se não houver essa informação.

Detalhe de cada campo novo de "nao_conformidades":
- categoria_nao_conformidade: área temática do item (ex: "Gestão de risco", "Sistema de drenagem",
  "Integridade estrutural", "Comunicação", "Atmosfera explosiva"), em 2-4 palavras.
- acao_recomendada: a ação que a ANP exige da operadora para sanar especificamente este item,
  em até 20 palavras (geralmente o próprio texto da notificação, ex: "Refazer o estudo de
  dispersão de gases considerando condição de calmaria").
- evidencias: ver seção "EVIDÊNCIAS" abaixo — evidências que fundamentam ESTA não conformidade
  específica (ex: registro fotográfico, laudo técnico, relatório citado como base do achado).

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

Preencha "respostas" (array de {{numero_item, resultado_final, decisao_anp, resumo, data_resposta,
evidencias}}) SOMENTE se tipo_documento for resposta_operadora ou parecer_anp. numero_item deve
corresponder exatamente ao número usado pelo próprio documento ao citar a não
conformidade/condicionante (ex: "5.4"). resumo em até 25 palavras. data_resposta é a data de
emissão/assinatura DESTE documento de resposta/parecer (formato AAAA-MM-DD se possível; use null se
não encontrar) — não confundir com a data da não conformidade original.
{dica_ncs_existentes}

EVIDÊNCIAS — tanto em "nao_conformidades" quanto em "respostas", cada não conformidade normalmente
tem VÁRIAS evidências. "evidencias" é um array de {{descricao, apresentado_por}} — capture TODAS as
evidências distintas relacionadas àquele item específico (use array vazio [] se não houver nenhuma
pra esse item — nunca invente uma evidência que não esteja no texto):
- descricao: PRIORIZE o dado concreto e específico já descrito no próprio texto — medições, datas de
  ocorrências, quantidades, resultados de teste, contagens, fotos/registros mencionados — em vez de um
  resumo genérico. Exemplos de BOA descricao: "9 vazamentos de gás detectados desde 2022, incluindo 1
  evento TIER 1 com CH4 e CO2 sem detecção por 2 detectores no nível HH", "Teste de dilúvio em
  12/2/2025 nos módulos 3P+2PW evidenciou subdimensionamento da drenagem", "Tanque de espuma com 68%
  do volume necessário (4.120 m³ de 5.700 m³, capacidade reduzida em 32%)". Só descreva a evidência
  como citação de outro documento (ex: "Carta DPBR-2024-11589 (4529951)", "Anexo 03 (4529954)") quando
  o texto realmente aponta pra outro documento como fonte da prova, e não há detalhe observacional
  direto disponível no próprio texto. Quando o texto citar um documento (Carta, Ofício, Anexo, Parecer,
  laudo) como fonte de uma evidência, SEMPRE inclua o número SEI entre parênteses quando houver (ex:
  "(4529951)", "(SEI nº 5093911)") — isso é usado para linkar a evidência ao arquivo certo depois.
- apresentado_por: "operadora" se a evidência foi submetida pela empresa fiscalizada (ex: Cartas
  da operadora, anexos técnicos enviados por ela em resposta a uma notificação); "anp" se foi a
  própria ANP quem produziu/observou a evidência (ex: registro da fiscalização, laudo do auditor,
  parecer técnico anterior). Use null só se o texto não deixar claro quem apresentou.
- Cuidado para não misturar evidências de itens diferentes: associe cada evidência apenas ao
  numero_item que o próprio texto vincula a ela, não à lista inteira de referências do documento.

Responda APENAS o JSON, começando com {{ e terminando com }}, sem nenhum texto antes ou depois.

TEXTO DO DOCUMENTO (pode estar truncado):
---
{texto}
---
"""


def build_prompt(texto, tipo_sei=None, nao_conformidades_existentes=None):
    dica = ""
    if tipo_sei:
        dica = (
            f'\nDica do sistema de origem (SEI): este documento foi catalogado lá como '
            f'"{tipo_sei}". Use isso como forte indício do tipo_documento correto, mas '
            f"confirme pelo conteúdo.\n"
        )

    dica_ncs = ""
    if nao_conformidades_existentes:
        linhas = "\n".join(
            f'- "{nc["numero_item"]}": {nc["descricao"]}'
            for nc in nao_conformidades_existentes if nc.get("numero_item")
        )
        if linhas:
            dica_ncs = (
                "NÃO CONFORMIDADES JÁ CADASTRADAS para este processo (extraídas do Documento de "
                "Fiscalização):\n"
                f"{linhas}\n"
                'Se este documento for resposta_operadora ou parecer_anp, o "numero_item" de cada item em '
                '"respostas" DEVE usar EXATAMENTE um dos numero_item da lista acima, escolhido pelo '
                "ASSUNTO tratado — mesmo que este documento organize sua própria análise com uma "
                'numeração diferente (ex: se este parecer usa "2.1", "2.2"... internamente mas isso não '
                "corresponde à numeração das notificações acima, IGNORE a numeração própria do documento "
                "e casa pelo assunto/conteúdo com o numero_item real da lista). Se o assunto de um item "
                "não corresponder a nenhuma não conformidade da lista, deixe numero_item como null em vez "
                "de inventar um número.\n"
            )

    return PROMPT.format(texto=texto, dica_tipo_sei=dica, dica_ncs_existentes=dica_ncs)


# ---------------------------------------------------------------------------
# Prompt de "re-casamento" — usado quando uma resposta/parecer já processado
# não bateu com nenhuma não conformidade pelo numero_item (documento com
# numeração própria, sem relação com a do DF) e a IA precisa decidir, só pelo
# conteúdo já extraído, a qual não conformidade real ela se refere. Muito mais
# barato que reprocessar o documento inteiro — não usa o texto do PDF, só o
# resumo/decisão já extraídos na primeira passada.
# ---------------------------------------------------------------------------
PROMPT_REMATCH = """Você organiza auditorias de segurança operacional da ANP (petróleo e gás, Brasil).

Uma resposta/parecer já foi processado e extraído, mas o numero_item que o próprio documento usa
("{numero_item_original}") não corresponde a nenhuma não conformidade cadastrada para este processo —
provavelmente porque o documento usa uma numeração própria (ex: da sua seção de análise interna), sem
relação com a numeração das notificações do Documento de Fiscalização.

Resumo já extraído desta resposta: {resumo}
Decisão da ANP: {decisao_anp}

Não conformidades cadastradas para este processo:
{lista_ncs}

Qual numero_item da lista acima corresponde ao ASSUNTO tratado nesta resposta? Responda SOMENTE com
JSON, sem markdown, no formato exato {{"numero_item": "string ou null"}} — use null se o assunto não
corresponder claramente a nenhuma da lista.
"""


def build_rematch_prompt(resposta, nao_conformidades_existentes):
    lista = "\n".join(
        f'- "{nc["numero_item"]}": {nc["descricao"]}'
        for nc in nao_conformidades_existentes if nc.get("numero_item")
    )
    return PROMPT_REMATCH.format(
        numero_item_original=resposta.get("numero_item") or "(nenhum)",
        resumo=resposta.get("resumo") or "(sem resumo)",
        decisao_anp=resposta.get("decisao_anp") or "(sem decisão registrada)",
        lista_ncs=lista or "(nenhuma)",
    )


# ---------------------------------------------------------------------------
# Prompt de "evidência de anexo" — usado quando um Documento de Fiscalização
# cita um anexo específico como fonte de mais detalhe pra uma não
# conformidade (ex: "os demais exemplos estão no anexo (SEI 4730233)"). Bem
# mais barato que o prompt principal: não reclassifica o documento, só
# extrai evidências relacionadas ao contexto já conhecido da NC.
# ---------------------------------------------------------------------------
PROMPT_ANEXO_EVIDENCIA = """Você organiza auditorias de segurança operacional da ANP (petróleo e gás, Brasil).

O Documento de Fiscalização deste processo citou o anexo abaixo como fonte de MAIS DETALHE para a
seguinte não conformidade:

Não conformidade: {contexto_nc}

Analise o texto do anexo e responda SOMENTE com JSON válido, sem markdown, no formato exato:
{{"evidencias": [{{"descricao": "string", "apresentado_por": "anp|operadora|null"}}]}}

Extraia TODAS as evidências específicas (observações, medições, datas, ocorrências, fotos
mencionadas) descritas neste anexo que sustentem essa não conformidade — uma entrada por evidência
distinta, priorizando o dado concreto (ex: "Vazamento ativo em linha de offloading, identificado em
[data/local]" em vez de um resumo genérico). Use array vazio [] se o anexo não tiver relação com a
não conformidade. Nunca invente uma evidência que não esteja no texto do anexo.

TEXTO DO ANEXO (pode estar truncado):
---
{texto}
---
"""


def build_anexo_evidencia_prompt(contexto_nc, texto_anexo):
    return PROMPT_ANEXO_EVIDENCIA.format(contexto_nc=contexto_nc or "(sem descrição)", texto=texto_anexo)
