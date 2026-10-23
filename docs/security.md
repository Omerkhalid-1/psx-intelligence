# Security

## Current controls (implemented and tested)

| Control | Where | Test |
|---|---|---|
| Secrets from environment only; `.env` gitignored; `.env.example` holds placeholders | `app/config.py` | `tests/unit/test_config.py` |
| Production refuses to start with weak/missing `SECRET_KEY` or `ADMIN_API_TOKEN` | `ProductionConfig.validate` | `test_production_requires_strong_secrets` |
| Admin/job endpoints require `Authorization: Bearer <ADMIN_API_TOKEN>`, constant-time compare, fail closed (503) if unset | `app/api/auth.py` | `tests/api/test_jobs.py` |
| Strict request validation (unknown fields rejected, bounded page sizes, enum checks) | Pydantic schemas | `test_submit_rejects_extra_fields_and_non_json`, `test_list_jobs_pagination_and_filters` |
| Only whitelisted job types can be dispatched | `JOB_TASKS` | `test_submit_unknown_job_type` |
| Generic 500 responses; exception details only in (redacted) logs | `register_error_handlers` | `test_internal_errors_do_not_leak_details` |
| Secret redaction in logs and stored job errors (DB/Redis URLs, bearer tokens, key=value secrets) | `app/utils/logging.py` | `test_logging.py`, `test_dispatch_failure_is_recorded` |
| Untrusted correlation IDs rejected unless short alphanumeric | `create_app` request hook | `test_correlation_id_is_propagated_when_safe` |
| Security headers: `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy` | after_request | `test_security_headers` |
| ORM / parameterised queries only. No raw SQL built from input | everywhere | code review |
| Audit trail for job submissions, in the same transaction | `services/audit.py` | `test_submit_runs_job_and_records_audit` |

## Known gaps (must close before exposure beyond a trusted network)

1. **Dashboard and read-only API are unauthenticated.** The System page shows job error
   messages (redacted). Deploy only on localhost/VPN, or behind a reverse proxy with
   authentication, until user auth exists (ADR-008).
2. **No rate limiting.** Planned for Phase 8 (Flask-Limiter backed by Redis, or at the proxy).
3. **No CSRF protection.** Currently no cookie-authenticated mutating forms exist. Required
   before adding any.
4. **Bootstrap is loaded from jsDelivr** without Subresource Integrity hashes. Add SRI or vendor
   the files in Phase 7.
5. **Document ingestion (Phase 4)** must treat PDFs as hostile: size limits, page limits,
   extraction in the worker only, no path components taken from input, timeouts.
6. **Outbound network restriction** for connectors is not yet in place (compose network policy /
   egress allowlist in Phase 8).

## psxdata-api service

- Runs as a separate process bound to `127.0.0.1` (or the compose network only). It has no
  authentication of its own, so never publish its port.
- Pinned (`v0.5.0` / image `0.5.0`). It is third-party code that scrapes PSX: review diffs before
  bumping it.
- The app treats its responses as untrusted: strict schema checks, Decimal parsing, value bounds,
  quarantined rejections, and a 50 MB response cap.
- psxdata sends a browser User-Agent to PSX. That is the library's behaviour, and the owner
  accepted the PSX-terms risk (ADR-012).

## Handling external content

Retrieved documents and news are data, never instructions. Any future LLM step receives content
inside delimited fields, produces schema-validated output, and has every number it emits checked
against the source record.

## Reporting

Never paste real credentials into issues, logs or chat. Rotate `ADMIN_API_TOKEN` by changing the
environment variable and restarting the API.
