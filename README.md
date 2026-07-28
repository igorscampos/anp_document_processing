# Painel de auditorias ANP — Microsoft Fabric

Pipeline que lê PDFs de auditoria de fiscalização da ANP, classifica/extrai dados
estruturados via LLM (Claude ou Fabric AI Functions) e grava o resultado num Fabric SQL
Database, rodando inteiramente dentro do Microsoft Fabric — sem UI própria. Documentos e
evidências (PDF original + HTML com âncora por parágrafo) ficam no OneLake; os resultados se
navegam por um relatório Power BI.

Esta é a versão Fabric-nativa do protótipo original (Streamlit + Supabase). A troca de
arquitetura foi deliberada: o app deixou de ter uma tela de upload interativa — arquivos são
processados em runs agendadas de um Data Pipeline, não na hora do upload. Veja o histórico do
repositório se precisar da versão Streamlit/Supabase anterior.

## Arquitetura

| Componente | Papel |
|---|---|
| Lakehouse (`Files/entrada`, `Files/evidencias`, `Files/evidencias-html`, `Files/src`) | Pasta de entrada (SEI CSV + PDFs + ZIPs), arquivos originais e HTML de evidência, e os módulos `.py` do pipeline |
| Fabric SQL Database | As 7 tabelas relacionais (`schema.tsql.sql`) |
| Notebook (`nb_pipeline_run.ipynb` + `fabric_pipeline.py`/`db.py`/`pipeline.py`) | Toda a lógica de classificação/extração e gravação |
| Data Pipeline agendado | Dispara o notebook periodicamente |
| Variable Library + conexão Key Vault | Configuração e a API key da Anthropic |
| Relatório Power BI | Navegação dos resultados (substitui as antigas abas do Streamlit) |

`pipeline.py` (extração de texto, regex, geração de HTML com âncora, catálogo CSV do SEI,
prompts) não depende de Fabric nem de banco — é lógica pura, testável fora do Fabric também.

## 1. Provisionar os itens do Fabric

1. Crie um workspace com capacidade paga **F2 ou superior** (necessário se for usar o
   caminho `fabric_ai`, e recomendado de qualquer forma).
2. Crie um **Lakehouse** e, dentro dele, as pastas `Files/entrada`, `Files/evidencias`,
   `Files/evidencias-html`, `Files/src`.
3. Publique `db.py`, `pipeline.py` e `fabric_pipeline.py` deste repositório em `Files/src/`.
4. Crie um **Fabric SQL Database** e rode `schema.tsql.sql` inteiro no editor T-SQL dele.
   **Antes de rodar**, confira a collation do banco — escolha uma variante
   case-insensitive/accent-insensitive (ex: `Latin1_General_100_CI_AI`), não a padrão do SQL
   Server, ou a busca por texto em português (nomes de operadora, `numero_relatorio`) pode
   falhar em casos com acentuação.
5. Crie a **Variable Library** com `LLM_PROVIDER`, `ANTHROPIC_MODEL`, `CLAUDE_MAX_CHARS`,
   `FABRIC_AI_MAX_CHARS`, `CONFIDENCE_THRESHOLD` (ver valores padrão em
   `fabric_pipeline.get_config`).
6. Crie uma conexão ligada ao **Azure Key Vault** guardando `ANTHROPIC_API_KEY` — nunca em
   texto plano na Variable Library ou no notebook.
7. Importe `nb_pipeline_run.ipynb` como Notebook do workspace, ajuste a célula de
   configuração pra puxar da Variable Library/Key Vault reais.
8. Crie um **Data Pipeline** com uma atividade Notebook apontando pra `nb_pipeline_run`,
   numa recorrência (ex: diária — não há necessidade de tempo real, já que o processamento
   deixou de ser interativo).
9. Construa o **relatório Power BI** sobre o Fabric SQL Database, uma página por tabela
   (`tb_auditorias`, `tb_nao_conformidades`, `tb_respostas`, `tb_evidencias`, e uma view/
   filtro de `tb_arquivos_processados` para `status IN ('pendente_vinculo','parcial','revisao_manual','erro')`).

## 2. Rodar

O pipeline roda inteiro dentro da run do Notebook (`fabric_pipeline.run_pipeline()`), sem
intervenção manual:
1. Aplica lotes da Batch API da Anthropic já concluídos de runs anteriores.
2. Resolve a fila de pendências (documentos vinculados fora de ordem).
3. Varre `Files/entrada/` — processa PDFs soltos e qualquer `.zip` (cada `.zip` deve ter uma
   pasta de 1º nível por processo de auditoria, PDFs soltos ou em subpastas dentro dela) e o
   catálogo CSV do SEI (colunas: nº processo sei, documento, tipo_documento, data_captura,
   status — **obrigatório**, é a fonte de verdade; qualquer arquivo fora dele é descartado
   sem chamar a IA).

Arquivos já processados não são removidos da pasta de entrada — o dedup por hash cuida disso
automaticamente (reprocessar o mesmo PDF é um no-op, sem gastar token de novo).

### Escolher o provedor de LLM

`LLM_PROVIDER` na Variable Library:
- `"claude"` (padrão) — API da Anthropic. PDFs soltos usam a API síncrona; ZIPs usam a Batch
  API (metade do custo por token, resultado aplicado numa run futura, pode levar até 24h).
- `"fabric_ai"` — Fabric AI Functions (`synapse.ml.aifunc`), sem custo de API key separada
  (cobrado via capacidade Fabric). Sem equivalente de Batch API — ZIPs processam em fila
  síncrona, arquivo por arquivo. Exige capacidade F2+ e o tenant switch de Copilot/Azure
  OpenAI habilitado — confirme com o admin do tenant antes de usar em produção.

### Forçar reprocessamento de um processo

Não há mais checkbox — é uma ação deliberada, direta no banco:
```sql
INSERT INTO tb_processos_reprocessar (numero_processo_anp) VALUES ('48610.221768/2025-20');
```
Só funciona automaticamente se o(s) arquivo(s) daquele processo ainda estiverem em
`Files/entrada/` (já que arquivos processados não são movidos por padrão) — se já tiverem
sido limpos de lá, é preciso soltar o PDF de novo na pasta antes da próxima run.

## Riscos conhecidos (confirmar antes de ir pra produção)

- **Egresso de rede** do Notebook para `api.anthropic.com` pode estar bloqueado por política
  de tenant — confirme com o admin antes de assumir Claude como caminho viável.
- **Fabric AI Functions** exige capacidade F2+ e o switch de Copilot/Azure OpenAI habilitado
  no tenant — sem isso, `LLM_PROVIDER="fabric_ai"` não funciona.
- **Links de evidência** (`link_arquivo`/`link_evidencia`) agora são caminhos OneLake, não
  URLs públicas — só abrem pra quem tem acesso ao workspace Fabric. Mais seguro que o bucket
  público do protótipo anterior, mas mais restritivo: confirme se auditores/operadoras
  externas dependiam do link público antes.
- **Driver ODBC 18 para SQL Server** pode não vir pré-instalado na imagem padrão do runtime
  do notebook — pode exigir um Fabric Environment customizado além do `%pip install pyodbc`.
- **Qualidade de extração do modelo padrão do Fabric AI Functions** nesses PDFs longos e
  densos em português não está validada — compare lado a lado com o caminho Claude antes de
  confiar nele em produção.
- **Migração de dados históricos** de uma instalação Supabase anterior não está automatizada
  aqui — os IDs são texto gerado em Python (`AUD-`/`NC-`/`RESP-`/`EVID-<hex>`), então são
  portáveis via export/import manual se for preciso preservar histórico.

## Desenvolvimento local

`pipeline.py` não tem dependência de Fabric/banco — dá pra testar a extração de texto, regex
de processo, geração de HTML com âncora e os prompts isoladamente. `db.py`/`fabric_pipeline.py`
precisam de `FABRIC_SQL_SERVER`/`FABRIC_SQL_DATABASE` (com uma identidade autenticada via
`DefaultAzureCredential`, ex: `az login`) e de `/lakehouse/default/Files` existir localmente
(ou aponte `LAKEHOUSE_FILES_ROOT` pra uma pasta local qualquer) pra rodar fora do Fabric.

```bash
pip install -r requirements.txt
```
