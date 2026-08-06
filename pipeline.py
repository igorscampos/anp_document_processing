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
def extract_pages(file_bytes):
    """Texto de cada página do PDF, sem truncar — uma entrada por página.

    Usado tanto para montar o texto completo (`extract_text`, truncado pro
    limite de tokens do prompt) quanto para gerar a versão HTML navegável do
    documento e localizar em qual página um item foi citado."""
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


def extract_text(file_bytes, max_chars):
    return "\n".join(extract_pages(file_bytes))[:max_chars]


def localizar_pagina_por_item(pages, numero_item):
    """Primeira página (1-based) que cita este numero_item como referência
    isolada (ex: "5.4") — usado pra ancorar o link de evidência na página
    certa do documento ORIGINAL (link_arquivo + fragmento "#page=N"; ver
    `_resolver_link_pagina` em streamlit_app.py). Heurística por regex (como
    `REGEX_SEI_REFERENCIADO`), não NLP:
    funciona porque o prompt já instrui a IA a extrair numero_item EXATAMENTE
    como aparece no texto do próprio documento. Devolve None se não achar
    (documento pode não repetir a numeração formal em lugar nenhum, ou o item
    veio de recasamento por assunto em vez do texto do próprio doc)."""
    if not numero_item:
        return None
    padrao = re.compile(r"(?<!\d)" + re.escape(numero_item) + r"(?!\d)")
    for i, texto_pagina in enumerate(pages, start=1):
        if padrao.search(texto_pagina or ""):
            return i
    return None


def _normaliza_busca(texto):
    return re.sub(r"\s+", " ", (texto or "")).strip()


def localizar_pagina_por_trecho(pages, trecho):
    """Primeira página (1-based) cujo texto contém este trecho verbatim —
    usado pra ancorar o link de evidência na página EXATA de onde ela foi
    extraída (mais preciso que `localizar_pagina_por_item`, que só acha a
    página onde o número do item é citado, não onde o texto da evidência em
    si está). Normaliza espaços/quebras de linha antes de comparar, já que
    pdfplumber pode quebrar linha de forma diferente do texto corrido que a
    IA viu. Trechos curtos demais dariam falso positivo, por isso o mínimo
    de caracteres. Devolve None se não achar (trecho pode ter sido levemente
    reescrito pela IA apesar da instrução, cruzar quebra de página, ou ser
    null quando a evidência não tem passagem direta de apoio no texto)."""
    trecho_norm = _normaliza_busca(trecho)
    if not trecho_norm or len(trecho_norm) < 8:
        return None
    for i, texto_pagina in enumerate(pages, start=1):
        if trecho_norm.lower() in _normaliza_busca(texto_pagina).lower():
            return i
    return None


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


def chave_ordenacao_arquivo(nome_arquivo):
    """Chave de ordenação numérica crescente pelo nome do arquivo — o nome é
    geralmente o número do documento no SEI, que cresce cronologicamente.
    Processar um lote nessa ordem ajuda um documento que cita/depende de
    outro do MESMO lote (ex: Parecer que cita o DF) resolver de primeira no
    processamento síncrono, sem precisar da fila de pendências. Cai pro nome
    como string se não for puramente numérico (defensivo — nem todo arquivo
    segue o padrão), sempre depois dos numéricos."""
    base = doc_id_from_filename(nome_arquivo)
    return (0, int(base)) if base.isdigit() else (1, base)


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
IDIOMA DOS CAMPOS: escreva os VALORES dos campos de texto livre listados abaixo (resumos,
descrições, categorias, ações, resultados, decisões etc.) EM INGLÊS, traduzindo do português do
documento original — os dados serão armazenados em inglês. Isso NÃO se aplica a "tipo_documento" nem
a "apresentado_por" (mantenha exatamente os valores literais especificados no formato). EXCEÇÕES que
devem permanecer exatamente como aparecem no documento original, SEM tradução: nomes próprios
(operadora, unidade_instalacao, auditor_responsavel, auditores_equipe); códigos, números e datas
(numero_processo_anp, numero_relatorio, codigo_auditoria_anp, ordem_servico, cnpj_operadora,
numero_item, norma_referencia, todas as datas); qualquer tag/código de equipamento dentro de
equipamento_evidencia (ex: mantenha "P-101", "PSV-204" intactos, mas pode traduzir o texto ao redor,
ex: "Bomba de incêndio P-101" -> "Fire pump P-101"); o número SEI entre parênteses citado dentro de
uma descricao de evidência (ex: mantenha "(4529951)" intacto mesmo traduzindo o texto ao redor, já
que esse número é extraído por regex depois); e o campo "trecho" de cada evidência, que deve ser
sempre uma cópia literal do texto original, no idioma original, nunca traduzido.

Preencha "auditoria" (objeto com codigo_auditoria_anp, ordem_servico, operadora, cnpj_operadora,
unidade_instalacao, tipo_auditoria, sumario_auditoria, acao_demandada, data_auditoria_inicio,
data_auditoria_fim, data_emissao_relatorio, auditor_responsavel, auditores_equipe, status_auditoria,
resultado_final, praticas_auditadas, documentos_solicitados, fonte_auditoria) SOMENTE se
tipo_documento for auditoria_oficial. O anexo técnico de uma auditoria também conta como
auditoria_oficial.

Preencha "nao_conformidades" (array de {{numero_item, descricao, categoria_nao_conformidade,
norma_referencia, classificacao_gravidade, acao_recomendada, prazo_correcao, severidade,
frequencia, elemento_critico, evidencias}}) SOMENTE se tipo_documento for auditoria_oficial E o
próprio documento for o Documento de Fiscalização (DF)
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
- praticas_auditadas: as práticas/temas de segurança operacional que a auditoria teve como objetivo
  verificar (ex: "Gestão de mudanças", "Investigação de incidentes", "Integridade mecânica",
  "Gerenciamento de risco de processo"), concatenadas separadas por "; ". null se o documento não
  detalhar isso (ex: um Ofício curto, sem escopo descrito).
- documentos_solicitados: documentos que a ANP solicitou/exigiu que a operadora apresentasse ou
  comprovasse (ex: "Procedimento de partida e parada", "Registros de treinamento da equipe",
  "Certificados de inspeção de válvulas de alívio"), concatenados separados por "; ". null se o
  documento não listar isso.
- fonte_auditoria: o órgão que originou/conduziu a auditoria — normalmente "ANP", mas pode ser
  "Marinha" (Autoridade Marítima/Marinha do Brasil) ou outro órgão explicitamente citado como
  responsável pela fiscalização. Use "ANP" como padrão se o texto não indicar outro órgão claramente.

Detalhe de cada campo novo de "nao_conformidades":
- categoria_nao_conformidade: área temática do item (ex: "Gestão de risco", "Sistema de drenagem",
  "Integridade estrutural", "Comunicação", "Atmosfera explosiva"), em 2-4 palavras.
- acao_recomendada: a ação que a ANP exige da operadora para sanar especificamente este item,
  em até 20 palavras (geralmente o próprio texto da notificação, ex: "Refazer o estudo de
  dispersão de gases considerando condição de calmaria").
- severidade: classificação de severidade/impacto do risco associado a este item, SE o documento
  usar ou citar uma matriz/classificação de risco explícita (ex: "Catastrófica", "Crítica",
  "Moderada", "Leve"). Diferente de classificacao_gravidade (a classificação geral do achado) —
  severidade é especificamente o eixo de IMPACTO de uma matriz de risco. null se o documento não
  classificar isso.
- frequencia: classificação de frequência/probabilidade de ocorrência do risco associado a este
  item, SE o documento usar ou citar uma matriz/classificação de risco explícita (ex: "Frequente",
  "Provável", "Ocasional", "Remota", "Improvável"). null se o documento não classificar isso.
- elemento_critico: o Elemento Crítico de Segurança Operacional (ECS/barreira de segurança)
  relacionado a este item, quando o documento identificar (ex: "Sistema de detecção de gás",
  "Válvula de alívio de pressão", "Sistema de combate a incêndio", "Sistema de parada de emergência
  (ESD)"). null se não identificado.
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
tem VÁRIAS evidências. "evidencias" é um array de {{descricao, trecho, apresentado_por,
sistema_evidencia, equipamento_evidencia, categoria_evidencia, descritivo_imagens}} — capture TODAS
as evidências distintas relacionadas àquele item específico (use array vazio [] se não houver nenhuma
pra esse item — nunca invente uma evidência que não esteja no texto):
- descricao: PRIORIZE o dado concreto e específico já descrito no próprio texto — medições, datas de
  ocorrências, quantidades, resultados de teste, contagens, fotos/registros mencionados — em vez de um
  resumo genérico. Exemplos de BOA descricao: "9 gas leaks detected since 2022, including 1 TIER 1
  event with CH4 and CO2 undetected by 2 detectors at the HH level", "Deluge test on 2/12/2025 in
  modules 3P+2PW showed drainage undersizing", "Foam tank at 68% of required volume (4,120 m³ of
  5,700 m³, capacity reduced by 32%)" (lembre-se: escreva descricao em inglês, ver instrução de
  IDIOMA DOS CAMPOS acima). Só descreva a evidência como citação de outro documento (ex: "Letter
  DPBR-2024-11589 (4529951)", "Anexo 03 (4529954)") quando o texto realmente aponta pra outro
  documento como fonte da prova, e não há detalhe observacional direto disponível no próprio texto.
  Quando o texto citar um documento (Carta, Ofício, Anexo, Parecer, laudo) como fonte de uma
  evidência, SEMPRE inclua o número SEI entre parênteses quando houver (ex: "(4529951)", "(SEI nº
  5093911)"), mantendo o número intacto — isso é usado para linkar a evidência ao arquivo certo
  depois.
- trecho: uma citação literal (8 a 30 palavras) copiada EXATAMENTE do texto do documento (mesmas
  palavras, mesma pontuação, SEMPRE no idioma original do documento — nunca traduza este campo, ao
  contrário de descricao) que contém o dado concreto usado em "descricao" — usado para localizar e
  recortar a página exata do PDF onde esta evidência aparece. Use null se não houver uma passagem
  contínua do texto que sustente diretamente essa evidência específica (ex: quando a evidência é só
  uma citação a outro documento, sem texto de apoio direto nesta página).
- apresentado_por: "operadora" se a evidência foi submetida pela empresa fiscalizada (ex: Cartas
  da operadora, anexos técnicos enviados por ela em resposta a uma notificação); "anp" se foi a
  própria ANP quem produziu/observou a evidência (ex: registro da fiscalização, laudo do auditor,
  parecer técnico anterior). Use null só se o texto não deixar claro quem apresentou.
- sistema_evidencia: o sistema operacional/de segurança ao qual esta evidência se refere (ex:
  "Sistema de drenagem", "Sistema de detecção e combate a incêndio", "Sistema elétrico", "Sistema
  de içamento"). null se não identificável no texto.
- equipamento_evidencia: o equipamento específico citado nesta evidência, se houver (ex: "Bomba de
  incêndio P-101", "Válvula PSV-204", "Detector de gás GD-12"). null se não identificável.
- categoria_evidencia: a natureza/tipo desta evidência (ex: "Registro fotográfico", "Laudo
  técnico", "Relatório de teste", "Documento", "Depoimento", "Medição"), em 1-3 palavras.
- descritivo_imagens: SE esta evidência envolver foto(s)/imagem(ns)/registro(s) visual(is) citados
  no texto, descreva o que a imagem mostra (ex: "Foto mostra corrosão acentuada na base do tanque,
  com placa de identificação visível"). null se a evidência não envolver imagem.
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
# Filtro local (Python, sem IA) de relevância de Anexo — decide CONTRA QUAIS
# não conformidades já cadastradas do processo um Anexo é candidato a ter
# relação, comparando vocabulário que já existe no banco pra cada NC com o
# texto do anexo. Só as NCs que baterem entram no prompt da IA; se nenhuma
# bater, a IA nem é chamada (decisão determinística, sem custo de token).
# ---------------------------------------------------------------------------
# Siglas/códigos técnicos (ex: "ESDV", "HAZOP", "QRA", "PSV-204", "CO2") —
# deliberadamente não usa lista de stopwords: só considera token específico
# em maiúsculas, nunca palavra genérica ("SISTEMA", "CONSIDERANDO" etc. não
# aparecem assim no texto corrido, então não geram falso positivo). Sem \b no
# final: extrai só o prefixo maiúsculo mesmo quando seguido de sufixo minúsculo
# (ex: "ESDVs" no plural, "HAZOPs") — o \b inicial já garante que o termo
# começa em borda de palavra.
REGEX_TERMO_TECNICO = re.compile(r"\b[A-ZÀ-Ú]{2,}[A-ZÀ-Ú0-9-]*")


def _termos_tecnicos(texto):
    return {t.upper() for t in REGEX_TERMO_TECNICO.findall(texto or "")}


def anexo_relevante_para_nc(texto_anexo, nc):
    """True se o anexo é candidato a ter relação com esta NC, por vocabulário
    já conhecido dela: frase de categoria_nao_conformidade ou elemento_critico
    aparecendo no texto do anexo, OU overlap de termos técnicos (siglas/
    códigos) entre a descricao da NC e o texto do anexo."""
    texto_lower = (texto_anexo or "").lower()
    for campo in (nc.get("categoria_nao_conformidade"), nc.get("elemento_critico")):
        if campo and campo.strip().lower() in texto_lower:
            return True
    termos_nc = _termos_tecnicos(nc.get("descricao") or "")
    if termos_nc and termos_nc & _termos_tecnicos(texto_anexo):
        return True
    return False


def filtrar_ncs_relevantes(texto_anexo, ncs):
    """Subconjunto de `ncs` (cada uma {numero_item, descricao,
    categoria_nao_conformidade, elemento_critico}, formato de
    `db.fetch_ncs_resumo`) que passa no filtro local de relevância — só essas
    entram no prompt da IA."""
    return [nc for nc in ncs if anexo_relevante_para_nc(texto_anexo, nc)]


# ---------------------------------------------------------------------------
# Prompt de "evidência de anexo" — usado pra ler um Anexo do processo contra
# UMA não conformidade candidata por vez (ver `filtrar_ncs_relevantes`), nunca
# a lista inteira numa chamada só. Um anexo grande com evidência pra várias
# NCs de uma vez gera uma resposta grande o bastante pra estourar max_tokens
# e voltar JSON truncado/inválido (já aconteceu — ver histórico de
# max_tokens em `_call_claude`); uma chamada pequena por NC candidata mantém
# cada resposta sempre dentro do limite — Anexo é raro no catálogo, e o
# filtro Python já reduz bastante quantas NCs viram chamada, então mais
# chamadas pequenas ainda sai barato.
# ---------------------------------------------------------------------------
PROMPT_ANEXO_EVIDENCIA = """Você organiza auditorias de segurança operacional da ANP (petróleo e gás, Brasil).

Este documento é um Anexo do processo, pré-selecionado (por vocabulário técnico em comum) como
CANDIDATO a sustentar a seguinte não conformidade já cadastrada:

Não conformidade {numero_item}: {descricao_nc}

Analise o texto do anexo e responda SOMENTE com JSON válido, sem markdown, no formato exato:
{{"evidencias": [{{"descricao": "string", "trecho": "string ou null",
"apresentado_por": "anp|operadora|null", "sistema_evidencia": "string ou null",
"equipamento_evidencia": "string ou null", "categoria_evidencia": "string ou null",
"descritivo_imagens": "string ou null"}}]}}

Confirme pelo conteúdo REAL do texto (não assuma só porque foi pré-selecionado) se este anexo
sustenta essa não conformidade específica. Se sim, extraia TODAS as evidências distintas
(observações, medições, datas, quantidades, resultados de teste, fotos mencionadas) — priorize
dado concreto sobre resumo genérico, mesmos critérios do prompt principal. Se o anexo não tiver
relação real com esta não conformidade, responda {{"evidencias": []}}. Nunca invente uma evidência
que não esteja no texto do anexo.

IDIOMA: escreva "descricao", "sistema_evidencia", "equipamento_evidencia" (exceto tags/códigos de
equipamento, ex: "P-101" — mantenha intactas), "categoria_evidencia" e "descritivo_imagens" EM
INGLÊS, traduzindo do português do anexo. "trecho" é a exceção: deve ser uma citação literal (8 a 30
palavras) copiada EXATAMENTE do texto do anexo, no idioma original, nunca traduzida — usada pra
recortar a página exata do PDF; use null se não houver passagem contínua de apoio direto. Mantenha
também intactos: qualquer número SEI entre parênteses citado dentro de descricao (ex: "(4529951)") e
o valor de apresentado_por (anp|operadora|null, sem tradução).

TEXTO DO ANEXO (pode estar truncado):
---
{texto}
---
"""


def build_anexo_evidencia_prompt(nc, texto_anexo):
    return PROMPT_ANEXO_EVIDENCIA.format(
        numero_item=nc.get("numero_item"),
        descricao_nc=nc.get("descricao") or "(sem descrição)",
        texto=texto_anexo,
    )
