# Crucible API routes

The resource identifier and lookup contract is documented in [`docs/resource_identifier_routes.md`](../../../docs/resource_identifier_routes.md).

Canonical single-resource GET routes use MFIDs, except people use ORCIDs. Projects and instruments can also be resolved by exact slug filters on their collection routes. Project paths and `GET /users/{user_id}` remain temporarily overloaded for compatibility.
