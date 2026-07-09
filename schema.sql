-- Rode este script no SQL Editor do seu projeto Supabase.
-- Se você já rodou a versão anterior deste schema, rode só o bloco "ALTER" no final.

create table if not exists tb_auditorias (
    id_auditoria text primary key,
    numero_processo_anp text,
    numero_relatorio text,
    codigo_auditoria_anp text,
    ordem_servico text,
    operadora text,
    cnpj_operadora text,
    unidade_instalacao text,
    tipo_auditoria text,
    sumario_auditoria text,
    acao_demandada text,
    data_auditoria_inicio text,
    data_auditoria_fim text,
    data_emissao_relatorio text,
    auditor_responsavel text,
    status_auditoria text,
    resultado_final text,
    arquivo_origem text,
    criado_em timestamptz default now(),
    atualizado_em timestamptz default now()
);

create table if not exists tb_nao_conformidades (
    id_nao_conformidade text primary key,
    id_auditoria text references tb_auditorias(id_auditoria),
    numero_item text,
    descricao text,
    categoria_nao_conformidade text,
    norma_referencia text,
    classificacao_gravidade text,
    acao_recomendada text,
    prazo_correcao text,
    status_atual text,
    arquivo_origem text,
    criado_em timestamptz default now(),
    atualizado_em timestamptz default now()
);

create table if not exists tb_respostas (
    id_resposta text primary key,
    id_nao_conformidade text references tb_nao_conformidades(id_nao_conformidade),
    id_auditoria text references tb_auditorias(id_auditoria),
    tipo_registro text check (tipo_registro in ('resposta_operadora','parecer_anp')),
    data_resposta text,
    texto_resposta text,
    acao_corretiva text,
    evidencias_anexadas text,
    decisao_anp text,
    justificativa_decisao text,
    resultado_final text,
    arquivo_origem text,
    criado_em timestamptz default now()
);

-- Controla arquivos já processados (evita gastar tokens de novo com o mesmo PDF)
-- e guarda uma fila de pendências: documentos cuja auditoria/item ainda não existe.
-- status possíveis: 'concluido', 'parcial', 'pendente_vinculo', 'revisao_manual', 'erro'
create table if not exists tb_arquivos_processados (
    hash_arquivo text primary key,
    nome_arquivo text,
    tipo_documento text,
    confianca numeric,
    status text,
    motivo text,
    payload_json jsonb,       -- guarda o resultado já extraído da IA para reprocessar sem gastar tokens de novo
    criado_em timestamptz default now(),
    atualizado_em timestamptz default now()
);

alter table tb_auditorias disable row level security;
alter table tb_nao_conformidades disable row level security;
alter table tb_respostas disable row level security;
alter table tb_arquivos_processados disable row level security;

-- ---------------------------------------------------------------------
-- ALTER: rode isto se você já tinha a versão anterior do schema aplicada
-- ---------------------------------------------------------------------
-- alter table tb_arquivos_processados add column if not exists payload_json jsonb;
-- alter table tb_arquivos_processados add column if not exists atualizado_em timestamptz default now();
-- alter table tb_auditorias add column if not exists codigo_auditoria_anp text;
-- alter table tb_auditorias add column if not exists ordem_servico text;
-- alter table tb_auditorias add column if not exists sumario_auditoria text;
-- alter table tb_auditorias add column if not exists acao_demandada text;
-- alter table tb_auditorias add column if not exists resultado_final text;
-- alter table tb_nao_conformidades add column if not exists categoria_nao_conformidade text;
-- alter table tb_nao_conformidades add column if not exists acao_recomendada text;
