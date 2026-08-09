# Memory Index

- [API state](project_api_state.md) — Current auth patterns, route design decisions, AssociatedFile/storage, metadata routes, graph endpoints
- [Hard deletion feature](project_hard_deletion.md) — PR #151, DeletionAuditLog, two-commit flow, next: add project_id to DeletionRequestRead
- [Pagination debt](project_pagination_debt.md) — Several list endpoints (e.g. GET /samples/{id}/datasets) return raw unpaginated lists; needs a future pass
- [Coding style feedback](feedback_coding_style.md) — No test sections in PRs; no trailing summaries; utilities must not commit
