-- Rode este script no editor T-SQL do seu Fabric SQL Database (item "SQL Database" no
-- workspace do Fabric). Porte de schema.sql (Postgres/Supabase) para T-SQL — mesmo desenho
-- de tabelas, mesmas 7 tabelas, mesmos PKs de texto gerados pela aplicação (AUD-/NC-/RESP-/
-- EVID-<10 hex chars>, via db.gen_id) — nenhuma IDENTITY/SERIAL envolvida.
--
-- IMPORTANTE ao provisionar o banco: escolha uma collation case-insensitive E
-- accent-insensitive (ex: Latin1_General_100_CI_AI) — não a padrão do SQL Server
-- (SQL_Latin1_General_CP1_CI_AS, que é sensível a acento). O texto em português com
-- acentuação (nomes de operadora, resumo, etc.) e a busca por numero_relatorio em
-- db.find_auditoria (equivalente ao "ilike" do Postgres) dependem disso pra casar
-- corretamente.
--
-- Row Level Security do Postgres não tem equivalente aqui nem é necessário: o acesso ao
-- Fabric SQL Database já é controlado por identidade Entra ID por padrão (só quem tem
-- permissão no workspace conecta) — isso é uma melhoria sobre o bucket público do Supabase
-- Storage do protótipo anterior, não uma lacuna a cobrir.

IF OBJECT_ID(N'dbo.tb_auditorias', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.tb_auditorias (
        id_auditoria nvarchar(50) NOT NULL PRIMARY KEY,
        numero_processo_anp nvarchar(max) NULL,
        numero_relatorio nvarchar(max) NULL,
        codigo_auditoria_anp nvarchar(max) NULL,
        ordem_servico nvarchar(max) NULL,
        operadora nvarchar(max) NULL,
        cnpj_operadora nvarchar(max) NULL,
        unidade_instalacao nvarchar(max) NULL,
        tipo_auditoria nvarchar(max) NULL,
        sumario_auditoria nvarchar(max) NULL,
        acao_demandada nvarchar(max) NULL,
        data_auditoria_inicio nvarchar(max) NULL,
        data_auditoria_fim nvarchar(max) NULL,
        data_emissao_relatorio nvarchar(max) NULL,
        auditor_responsavel nvarchar(max) NULL,
        auditores_equipe nvarchar(max) NULL,  -- demais integrantes da equipe (além do líder), concatenados com "; "
        status_auditoria nvarchar(max) NULL,
        resultado_final nvarchar(max) NULL,
        arquivo_origem nvarchar(max) NULL,
        arquivo_origem_score int NOT NULL DEFAULT 0,  -- "riqueza" do arquivo_origem atual (qtd de NCs + campos preenchidos) — só troca de DF de referência quando um novo tiver score maior
        data_emissao_origem date NULL,  -- data de emissão do documento que gravou os campos atuais — só sobrescreve com um documento igual ou mais recente
        criado_em datetime2 NOT NULL DEFAULT sysutcdatetime(),
        atualizado_em datetime2 NOT NULL DEFAULT sysutcdatetime()
    )
END

IF OBJECT_ID(N'dbo.tb_nao_conformidades', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.tb_nao_conformidades (
        id_nao_conformidade nvarchar(50) NOT NULL PRIMARY KEY,
        id_auditoria nvarchar(50) NULL CONSTRAINT FK_nc_auditoria REFERENCES dbo.tb_auditorias(id_auditoria),
        numero_item nvarchar(max) NULL,
        descricao nvarchar(max) NULL,
        categoria_nao_conformidade nvarchar(max) NULL,
        norma_referencia nvarchar(max) NULL,
        classificacao_gravidade nvarchar(max) NULL,
        acao_recomendada nvarchar(max) NULL,
        prazo_correcao nvarchar(max) NULL,
        status_atual nvarchar(max) NULL,
        arquivo_origem nvarchar(max) NULL,
        data_emissao_origem date NULL,  -- data de emissão do documento (DF) que gravou os campos descritivos atuais
        status_data_origem date NULL,   -- data de emissão do documento (resposta/parecer) que gravou o status_atual atual
        criado_em datetime2 NOT NULL DEFAULT sysutcdatetime(),
        atualizado_em datetime2 NOT NULL DEFAULT sysutcdatetime()
    )
END

IF OBJECT_ID(N'dbo.tb_respostas', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.tb_respostas (
        id_resposta nvarchar(50) NOT NULL PRIMARY KEY,
        id_nao_conformidade nvarchar(50) NULL CONSTRAINT FK_resp_nc REFERENCES dbo.tb_nao_conformidades(id_nao_conformidade),
        id_auditoria nvarchar(50) NULL CONSTRAINT FK_resp_auditoria REFERENCES dbo.tb_auditorias(id_auditoria),
        tipo_registro nvarchar(50) NULL CONSTRAINT CK_resp_tipo_registro CHECK (tipo_registro IN ('resposta_operadora', 'parecer_anp')),
        data_resposta nvarchar(max) NULL,
        texto_resposta nvarchar(max) NULL,
        acao_corretiva nvarchar(max) NULL,
        evidencias_anexadas nvarchar(max) NULL,  -- superseded por tb_evidencias (1 evidência por linha, com origem); mantida só por compatibilidade, o app não escreve mais aqui
        decisao_anp nvarchar(max) NULL,
        justificativa_decisao nvarchar(max) NULL,
        resultado_final nvarchar(max) NULL,
        arquivo_origem nvarchar(max) NULL,
        criado_em datetime2 NOT NULL DEFAULT sysutcdatetime()
    )
END

-- Uma não conformidade ou uma resposta pode ter VÁRIAS evidências, cada uma
-- apresentada pela ANP ou pela operadora — por isso é tabela própria (1 linha
-- por evidência) em vez de um campo de texto único nas tabelas pai. Cada
-- linha se liga a EXATAMENTE UMA das duas (id_nao_conformidade OU
-- id_resposta, nunca as duas ao mesmo tempo nem nenhuma das duas).
IF OBJECT_ID(N'dbo.tb_evidencias', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.tb_evidencias (
        id_evidencia nvarchar(50) NOT NULL PRIMARY KEY,
        id_nao_conformidade nvarchar(50) NULL CONSTRAINT FK_evid_nc REFERENCES dbo.tb_nao_conformidades(id_nao_conformidade),
        id_resposta nvarchar(50) NULL CONSTRAINT FK_evid_resposta REFERENCES dbo.tb_respostas(id_resposta),
        apresentado_por nvarchar(20) NULL CONSTRAINT CK_evid_apresentado_por CHECK (apresentado_por IN ('anp', 'operadora')),
        descricao nvarchar(max) NULL,
        sistema_evidencia nvarchar(max) NULL,
        equipamento_evidencia nvarchar(max) NULL,
        categoria_evidencia nvarchar(max) NULL,
        pagina_evidencia int NULL,  -- página do documento-fonte onde a âncora foi localizada
        tipo_link_evidencia nvarchar(30) NULL CONSTRAINT CK_evid_tipo_link CHECK (tipo_link_evidencia IN ('ancora_precisa', 'ancora_pagina', 'documento_completo')),
        arquivo_referenciado nvarchar(max) NULL,  -- nº SEI citado dentro da própria descricao (regex), se houver
        link_evidencia nvarchar(max) NULL,  -- HTML do documento-fonte (com #âncora do parágrafo) quando localizável; senão caminho OneLake do PDF inteiro
        arquivo_origem nvarchar(max) NULL,
        criado_em datetime2 NOT NULL DEFAULT sysutcdatetime(),
        -- Postgres usava (a IS NOT NULL)::int + (b IS NOT NULL)::int = 1 — T-SQL não tem cast
        -- booleano-pra-int, então reescreve com CASE WHEN.
        CONSTRAINT CK_evid_um_pai_so CHECK (
            (CASE WHEN id_nao_conformidade IS NOT NULL THEN 1 ELSE 0 END)
            + (CASE WHEN id_resposta IS NOT NULL THEN 1 ELSE 0 END) = 1
        )
    )
END

-- Controla quais processos foram marcados pra forçar releitura e reescrita.
-- Cada linha é removida automaticamente depois que o(s) arquivo(s) daquele
-- processo forem reprocessados (marca de "um disparo só").
IF OBJECT_ID(N'dbo.tb_processos_reprocessar', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.tb_processos_reprocessar (
        numero_processo_anp nvarchar(200) NOT NULL PRIMARY KEY,
        criado_em datetime2 NOT NULL DEFAULT sysutcdatetime()
    )
END

-- Controla arquivos já processados (evita gastar tokens de novo com o mesmo PDF)
-- e guarda uma fila de pendências: documentos cuja auditoria/item ainda não existe.
-- status possíveis: 'concluido', 'parcial', 'pendente_vinculo', 'revisao_manual', 'erro'
IF OBJECT_ID(N'dbo.tb_arquivos_processados', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.tb_arquivos_processados (
        hash_arquivo nvarchar(64) NOT NULL PRIMARY KEY,  -- SHA-256 hex = 64 chars
        nome_arquivo nvarchar(max) NULL,
        tipo_documento nvarchar(max) NULL,
        confianca float NULL,
        status nvarchar(30) NULL,
        motivo nvarchar(max) NULL,
        payload_json nvarchar(max) NULL CONSTRAINT CK_arq_payload_json CHECK (payload_json IS NULL OR ISJSON(payload_json) = 1),  -- guarda o resultado já extraído da IA para reprocessar sem gastar tokens de novo
        numero_sei nvarchar(max) NULL,                    -- número do documento no SEI (coluna "documento" do catálogo CSV)
        numero_processo_anp nvarchar(max) NULL,           -- número de processo vindo do catálogo CSV (fonte de verdade, não regex/IA)
        numero_processo_divergente nvarchar(max) NULL,    -- número diferente encontrado no texto/pasta, se houver, só para auditoria
        link_arquivo nvarchar(max) NULL,                  -- caminho OneLake deste arquivo — permite achar o link de um documento pelo numero_sei depois
        data_emissao_documento date NULL,        -- data de emissão do próprio documento, extraída pela IA do conteúdo (normalizada para ISO)
        criado_em datetime2 NOT NULL DEFAULT sysutcdatetime(),
        atualizado_em datetime2 NOT NULL DEFAULT sysutcdatetime()
    )
END

-- Controla lotes enviados à Batch API da Anthropic (arquivos soltos na pasta de entrada do
-- OneLake, um lote por .zip encontrado, ou um lote por rodada de PDFs soltos) — permite
-- conferir o status numa run futura do pipeline e aplicar os resultados só quando o lote
-- estiver pronto (a Batch API pode levar até 24h, embora a maioria termine em menos de 1h).
IF OBJECT_ID(N'dbo.tb_lotes_batch', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.tb_lotes_batch (
        id_lote nvarchar(100) NOT NULL PRIMARY KEY,  -- id do batch na Anthropic (msgbatch_...)
        status nvarchar(30) NULL,                     -- 'in_progress' | 'canceling' | 'ended' | 'concluido'
        total_itens int NULL,
        itens_json nvarchar(max) NULL CONSTRAINT CK_lote_itens_json CHECK (itens_json IS NULL OR ISJSON(itens_json) = 1),  -- custom_id -> {nome_arquivo, hash_arquivo, pasta_processo, tipo_sei, decisao_catalogo, numero_processo_final, numero_processo_divergente, link_arquivo}
        nome_arquivo_zip nvarchar(max) NULL,           -- nome do .zip de origem — cada zip enviado vira um lote independente
        criado_em datetime2 NOT NULL DEFAULT sysutcdatetime(),
        atualizado_em datetime2 NOT NULL DEFAULT sysutcdatetime()
    )
END

-- Para adicionar uma coluna nova a uma tabela existente sem quebrar deploys repetidos deste
-- script, use o padrão idempotente abaixo (equivalente ao "add column if not exists" do
-- Postgres) em vez de "ALTER TABLE ... ADD" direto:
--
-- IF COL_LENGTH('dbo.tb_auditorias', 'nome_da_coluna_nova') IS NULL
-- BEGIN
--     ALTER TABLE dbo.tb_auditorias ADD nome_da_coluna_nova nvarchar(max) NULL
-- END
