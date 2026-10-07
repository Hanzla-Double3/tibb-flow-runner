# Security policy

## Supported versions

Security fixes are applied to the current development version of this repository.

## Reporting a vulnerability

Please do not open a public issue for a suspected security vulnerability. Contact the repository owner privately with:

- a concise description of the issue;
- reproduction steps or a proof of concept;
- the affected version or commit; and
- any suggested mitigation, if known.

Do not include API keys, authentication tokens, patient data, or other sensitive data in the report. The maintainer will acknowledge the report, assess impact, and coordinate a fix and disclosure timeline.

## Handling test data

This tool can execute API flows against configured services. Use only controlled test environments and synthetic data. Store credentials locally in `.env` or workspace secrets, never in source files or committed flow definitions.
