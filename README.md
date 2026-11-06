# Milo PDF Loop

Milo is a standalone, local-first PDF extraction and feedback system. It converts
PDFs to Markdown or structured JSON, scores extraction quality, records feedback,
and reprocesses weak results with alternate decoders.

This repository is a sanitized extraction of the Milo functionality. It contains
no model files, databases, documents, credentials, deployment hostnames, or
environment-specific paths.

## What “learning” means

Milo does not update model weights. It records decoder outcomes and quality scores
in a local database. Later runs rank available PDF decoders by their observed
quality and use that history to choose a better decoder order. Low-quality
documents enter a bounded reprocessing loop.

Flow:

1. Extract with pdfplumber, PyMuPDF, or pypdf.
2. Score extraction quality with offline heuristics.
3. Optionally ask an explicitly configured OpenAI-compatible quality endpoint.
4. Record decoder outcomes without storing matched secret values.
5. Queue weak results for at most three enhanced retries.
6. Prefer historically stronger decoders on subsequent documents.

## Safety defaults

- The API binds to loopback by default.
- Binding outside loopback requires a bearer token.
- Uploaded PDFs, generated output, databases, logs, models, and environment files
  are ignored by Git.
- API uploads are size-limited and must begin with a PDF signature.
- Output metadata reports sensitive-data categories and counts, never matched values.
- No external inference endpoint is contacted unless MILO_LLM_BASE_URL is set.
- The configured API key is never written to the database or logs.

## Quick start

~~~powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
milo init
milo convert .\document.pdf --format json --enhanced
pytest
~~~

On Linux or macOS, activate the environment with:

~~~bash
source .venv/bin/activate
~~~

Run the local API:

~~~bash
milo serve
~~~

The default endpoint is http://127.0.0.1:8091. Interactive API documentation is
available at /docs.

## CLI

~~~text
milo init
milo convert INPUT.pdf [--format markdown|json] [--enhanced]
milo feedback DOCUMENT --type low_quality --reason "table extraction failed" --score 0.3
milo reprocess [--limit 10]
milo stats
milo decoders
milo serve [--host 127.0.0.1] [--port 8091]
~~~

## API

- GET /health
- POST /v1/convert
- POST /v1/feedback
- POST /v1/reprocess
- GET /v1/stats
- GET /v1/decoders

When MILO_API_TOKEN is configured, send:

~~~text
Authorization: Bearer YOUR_TOKEN
~~~

## Container

Set MILO_API_TOKEN in your shell, then:

~~~bash
docker compose up --build
~~~

The compose file publishes only to 127.0.0.1:8091 and stores runtime data in a
named volume.

## Publishing checklist

1. Replace the all-rights-reserved LICENSE only if you intentionally choose a
   different license.
2. Run pytest and ruff check .
3. Run detect-secrets scan --all-files.
4. Review git status and git diff --cached before every push.
5. Never force-add ignored PDFs, databases, .env files, model files, or logs.
