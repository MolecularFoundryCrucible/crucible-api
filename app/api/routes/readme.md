# Crucible API routes

The resource identifier and lookup contract is documented in [`docs/resource_identifier_routes.md`](../../../docs/resource_identifier_routes.md).

Path segments named `{x_ref}` accept every identifier form for their entity: MFID or slug for projects and instruments, ORCID, service-account MFID, or username for users, and MFID or username for service accounts. Path segments named `{x_mfid}` or `{mfid}` accept the MFID only.
