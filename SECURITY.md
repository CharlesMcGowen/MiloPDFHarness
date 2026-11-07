# Security policy

## Sensitive documents

Milo processes PDFs locally and generated output may contain the full document
text. Keep the runtime directory private, encrypt storage where appropriate, and
do not commit generated files.

## Inference endpoints

MILO_LLM_BASE_URL is disabled by default. Configuring it may send a bounded text
excerpt to that endpoint. Only use an endpoint authorized for the document data.

## Optional adapters

Camelot and Docling are disabled by default and run locally when explicitly
invoked. Install them in a dedicated environment, pin and audit transitive
dependencies, and review model licenses. Docling may download model artifacts on
its first run; prefetch and checksum those artifacts for controlled or air-gapped
deployments.

## API exposure

Use the default loopback binding. The CLI refuses a non-loopback bind unless
MILO_API_TOKEN is set. Put TLS and additional access controls in front of the API
before exposing it beyond a trusted host.

## Reporting

Report suspected credential disclosure or unsafe document handling privately to
the repository owner. Do not include live credentials or confidential document
content in an issue.
