"""Lógica de extração de texto, catálogo de tipos de documento (CSV do SEI) e
prompt de classificação — compartilhada entre o app Streamlit (streamlit_app.py)
e scripts de teste/linha de comando.

O CSV do SEI (nº processo sei, documento, tipo_documento, data_captura, status)
já traz o tipo real de cada documento, atribuído pelo próprio sistema de origem.
Usamos essa informação para duas coisas, ANTES de chamar a IA:

1. Descartar de cara arquivos com erro de captura ou cujo tipo nunca carrega
   conteúdo de auditoria/não-conformidade (e-mail, recibo de protocolo, anexo
   solto, despacho de trâmite etc.) — evita gastar tokens à toa.
2. Marcar com certeza os documentos que SÃO o Documento de Fiscalização (ou
   peça equivalente da família de fiscalização) como "auditoria_oficial",
   em vez de confiar na IA para adivinhar isso — essa era a principal fonte
   de classificação errada.
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


def _norm_tipo(tipo):
    return (tipo or "").strip().lower()


def load_catalog(csv_source):
    """Lê o CSV nº processo sei / documento / tipo_documento / status.

    Aceita um caminho de arquivo (str) ou um objeto file-like já aberto em
    modo texto (ex: io.StringIO, ou o retorno de um file_uploader do Streamlit).

    Retorna dict {numero_documento(str): {"processo", "tipo_documento", "status"}}.
    """
    catalogo = {}

    def _parse(f):
        reader = csv.DictReader(f)
        for row in reader:
            doc_id = (row.get("documento") or "").strip()
            if not doc_id:
                continue
            catalogo[doc_id] = {
                "processo": (row.get("nº processo sei") or "").strip(),
                "tipo_documento": (row.get("tipo_documento") or "").strip(),
                "status": (row.get("status") or "").strip(),
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
    - "descartar": não chama a IA, arquivo é só ruído administrativo ou falhou
      a captura no SEI.
    - "prioritario": chama a IA só para extrair campos, mas o tipo_documento
      final é forçado para "auditoria_oficial" (não depende da IA classificar).
    - "processar": segue o fluxo normal, IA decide o tipo_documento.

    Se o arquivo não está no catálogo (ex: CSV não cobre esse processo), cai
    em "processar" — comportamento antigo, sem filtro algum.
    """
    if not entry:
        return "processar"
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


def build_prompt(texto, tipo_sei=None):
    dica = ""
    if tipo_sei:
        dica = (
            f'\nDica do sistema de origem (SEI): este documento foi catalogado lá como '
            f'"{tipo_sei}". Use isso como forte indício do tipo_documento correto, mas '
            f"confirme pelo conteúdo.\n"
        )
    return PROMPT.format(texto=texto, dica_tipo_sei=dica)
