# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Streamlit app that ingests PDF audit documents from ANP (Brazilian oil & gas
regulator) exported from the SEI system, classifies/extracts structured data
from them via an LLM (Claude Haiku by default, or a local Ollama model), and
writes the results into Postgres (Supabase). All UI text, comments, and
prompts are in Portuguese — keep new code consistent with that.

## Running the app

```bash
python -m venv venv && source venv/bin/activate   # or `uv sync` (pyproject.toml is uv-managed)
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Opens at `http://localhost:8501`. There is no test suite, linter, or CI
config in this repo.

### Configuration

Credentials come from `.streamlit/secrets.toml` (see `README.md` for the
setup flow) or equivalent environment variables, read via `get_secret()` in
`streamlit_app.py`:

- `SUPABASE_URL`, `SUPABASE_KEY` — required always.
- `LLM_PROVIDER` — `"claude"` (default) or `"ollama"`. This is the one line
  to flip to switch model backends.
- `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` (default `claude-haiku-4-5-...`) —
  required when `LLM_PROVIDER=claude`.
- `OLLAMA_HOST`, `OLLAMA_MODEL`, `OLLAMA_MAX_CHARS`, `OLLAMA_NUM_CTX`,
  `OLLAMA_NUM_PREDICT`, `OLLAMA_THINK` — used when `LLM_PROVIDER=ollama`.
  `OLLAMA_THINK` must stay `false` for reasoning models (qwen3,
  deepseek-r1...) or the JSON response ends up in the `thinking` field
  instead of `response` and the app breaks.
- `CLAUDE_MAX_CHARS` — char budget for the extracted PDF text sent to Claude.

Database schema lives in `schema.sql` — run it once in the Supabase SQL
Editor. It's additive/idempotent (`create table if not exists`); if you add
a column, add both the `create table` definition AND a matching
`alter table ... add column if not exists` line in the `ALTER` block at the
bottom, so existing databases can be upgraded by re-running the file.

RLS is disabled on all tables in this prototype (see `schema.sql` and
`README.md` §5) — do not silently "fix" that as a drive-by change.

## Architecture

Three files carry all the logic:

- **`pipeline.py`** — pure functions, no I/O to Supabase or the LLM SDKs
  (only PDF parsing via `pdfplumber`/`pypdfium2` and CSV parsing). Owns: the
  SEI document-type catalog (`TIPOS_DESCARTAR`, `TIPOS_PRIORITARIOS`,
  `TIPOS_DF`), the classification prompt (`PROMPT`/`build_prompt`), the
  rematch prompt (`PROMPT_REMATCH`), the anexo-evidence prompt
  (`PROMPT_ANEXO_EVIDENCIA`), process-number regex extraction, and
  page-locating helpers for evidence links. This is the file to read first
  to understand the domain rules — its module docstring and section
  comments explain *why* each rule exists.
- **`db.py`** — all Supabase reads/writes, one function per table
  operation (`upsert_auditoria`, `upsert_nc`, `insert_resposta`,
  `insert_evidencias`, ...) plus Storage upload helpers for PDF evidence
  files.
- **`streamlit_app.py`** — orchestration + UI. Calls into `pipeline.py` and
  `db.py`; owns the LLM call wrappers (`_call_claude`/`_call_ollama`), the
  per-file processing pipeline, batch/ZIP handling, and the Streamlit
  widgets at the bottom of the file.

### The SEI catalog is the source of truth

The uploaded CSV catalog (columns: `nº processo sei`, `documento`,
`tipo_documento`, `status`; English aliases like `process_number` are also
accepted — see `pipeline._ALIAS_COLUNAS`) gates everything **before** any
LLM call:

1. A file not mapped in the catalog, or whose `tipo_documento` is in
   `TIPOS_DESCARTAR`, or whose capture `status` isn't `"sucesso"`, is
   discarded without spending tokens (`classify_catalog_entry` →
   `"descartar"`).
2. A file whose `tipo_documento` is in `TIPOS_PRIORITARIOS` is forced to
   `tipo_documento = "auditoria_oficial"` regardless of what the LLM
   returns (`"prioritario"`); only `TIPOS_DF` (a subset of
   `TIPOS_PRIORITARIOS`) is treated as the actual Documento de Fiscalização
   that non-conformities (`nao_conformidades`) are extracted from — the
   Ordem de Serviço/Plano/Relatório siblings enrich the audit record but
   never produce NC rows.
3. The process number (`numero_processo_anp`) always comes from the
   catalog, never from LLM output or regex — a different number found in
   the document text is recorded only as `numero_processo_divergente`, for
   audit purposes, and never used for linking.

The filename (minus extension) is the SEI document number and the join key
into the catalog (`doc_id_from_filename`).

### Data model / linking flow

Four tables, all in `schema.sql`: `tb_auditorias` → `tb_nao_conformidades` →
`tb_respostas` → `tb_evidencias` (evidence belongs to exactly one NC *or*
one resposta, enforced by a check constraint). Two auxiliary tables:
`tb_arquivos_processados` (dedup by SHA-256 hash + a pending-link queue) and
`tb_lotes_batch` (Batch API job tracking). A `tb_processos_reprocessar`
table lets a user mark specific process numbers for forced reprocessing
without checking the global "force" checkbox.

Uploads can arrive in **any order** — a resposta/parecer PDF may reach the
app before the Documento de Fiscalização it responds to exists yet:

- If the target NC (matched by `numero_item`) isn't found, the extracted
  result is stashed as JSON in `tb_arquivos_processados.payload_json` with
  status `pendente_vinculo`/`parcial`, and `retry_pending_queue()` is
  re-run after every new `auditoria_oficial` is written to try resolving
  the backlog — no re-call to the LLM needed.
- If `numero_item` doesn't match any existing NC exactly (the document may
  use its own internal numbering unrelated to the DF's), a small,
  cheap "rematch" LLM call (`match_resposta_to_nc` /
  `PROMPT_REMATCH`) asks the model to pick the right NC by subject, using
  only the already-extracted summary — never the full PDF text again.
- "Anexo" (attachment) documents are handled specially
  (`_try_processar_anexo`): a local, non-LLM vocabulary filter
  (`pipeline.filtrar_ncs_relevantes`) narrows down which existing NCs an
  attachment is plausibly evidence for, and only those candidates get one
  LLM call *each* (never the whole NC list in one call — a big response
  risks truncating past `max_tokens`; see `ANEXO_CLAUDE_MAX_TOKENS`).

Evidence links point at a single cropped PDF page (`pipeline.build_page_pdf`
+ `db.upload_evidencia_pagina`), not a full document or an HTML
reconstruction, so tables/stamps/layout stay intact and the text stays
selectable. `db.insert_evidencias` also regex-extracts a referenced SEI
number from the evidence description text (`REGEX_SEI_REFERENCIADO`) to
link evidence to the actual source document instead of the citing document.

### Two processing paths for batches (ZIP upload)

Chosen automatically by `LLM_PROVIDER`:

- **Claude** → Anthropic **Batch API** (`prepare_batch_from_zip` +
  `submit_batch`): half the per-token cost, async (up to ~24h, usually
  much faster). Results are pulled later via `process_pending_batches` /
  `apply_batch_result`, which re-downloads the original PDF from Storage
  (via the saved `link_arquivo`) since the batch may resolve hours later
  with no bytes left in memory.
- **Ollama** (no batch API) → synchronous per-file queue
  (`process_zip_batch_sync`), also usable with Claude if you want immediate
  results instead of waiting on the Batch API.

Within a ZIP, the first path segment is the process folder; files across a
process are sorted by `pipeline.chave_ordenacao_arquivo` (numeric filename
order, since SEI document numbers increase chronologically) so a
same-batch resposta/parecer has the best chance of resolving against its DF
immediately instead of falling into the pending queue.

`finalize_result()` in `streamlit_app.py` is the single choke point where
all three paths (direct upload, sync ZIP queue, batch-API result
application) converge to apply the confidence gate
(`CONFIDENCE_THRESHOLD = 0.7`), catalog overrides, and DB writes — if you're
changing how a classified result gets persisted, that's the function to
edit, not each caller.
