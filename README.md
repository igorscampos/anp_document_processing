# Painel de auditorias ANP — Streamlit + Supabase

App com upload de PDF, classificação/extração via Claude Haiku e gravação em
Postgres (Supabase), com custo próximo de zero em volume baixo/médio.

## 1. Criar o banco (Supabase — gratuito)

1. Crie uma conta em [supabase.com](https://supabase.com) e um novo projeto (tier Free).
2. Vá em **SQL Editor > New query**, cole o conteúdo de `schema.sql` e rode.
3. Vá em **Project Settings > API** e copie:
   - `Project URL` → vai virar `SUPABASE_URL`
   - `service_role` key (ou `anon` key, se ativar RLS depois) → vai virar `SUPABASE_KEY`

## 2. Configurar as credenciais

Copie `.streamlit/secrets.toml.example` para `.streamlit/secrets.toml` e
preencha com sua chave da Anthropic e as credenciais do Supabase.

**Nunca** commite o `secrets.toml` de verdade num repositório público — ele já
está no formato que o Streamlit lê automaticamente, tanto local quanto no
Community Cloud (lá você cola o conteúdo na aba "Secrets" das configurações
do app, em vez de subir o arquivo).

## 3. Rodar localmente

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Abre em `http://localhost:8501`.

## 4. Publicar de graça (Streamlit Community Cloud)

1. Suba esta pasta para um repositório no GitHub (pode ser privado).
2. Vá em [share.streamlit.io](https://share.streamlit.io), conecte sua conta
   do GitHub e escolha o repositório + `app.py` como arquivo principal.
3. Em **Advanced settings > Secrets**, cole o conteúdo do seu
   `.streamlit/secrets.toml` real.
4. Deploy. Você recebe uma URL pública (`seu-app.streamlit.app`) que qualquer
   pessoa da equipe pode acessar para fazer upload dos PDFs.

Isso cobre hospedagem, upload, processamento e banco por **US$ 0/mês** de
infraestrutura — o único custo real é o consumo de tokens da API da Anthropic.

## 5. Segurança antes de usar com dados sensíveis

Este protótipo desativa Row Level Security (RLS) no Supabase para simplificar
os testes. Antes de expor publicamente com dados reais de auditoria:

- Ative RLS nas 4 tabelas e crie policies restringindo leitura/escrita.
- Considere colocar autenticação no app (Streamlit Community Cloud tem opção
  de restringir acesso por e-mail/domínio nas configurações do app).
- Use a `anon key` do Supabase (não a `service_role`) se o app for público,
  e restrinja permissões via RLS em vez de confiar só na chave.

## 6. Próximos passos possíveis

- Trocar chamadas síncronas pela **Batch API** da Anthropic para cortar mais
  50% do custo — exige um worker separado que envia os lotes e outro que
  busca os resultados depois (não cabe no modelo simples de "upload e vê na
  hora" do Streamlit, mas dá para rodar como um cron job à parte enquanto o
  Streamlit só lê os resultados já prontos do banco).
- Adicionar OCR (`pytesseract`) para PDFs escaneados como imagem.
- Adicionar autenticação de usuário nativa do Streamlit ou do Supabase Auth.
