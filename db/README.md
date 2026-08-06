# db

Postgres schema for the `smi_agent` service (conversations, trips, tenancy,
config, investigations). Separate from Temporal's own Postgres schema, which
lives in its own `temporal-postgres` container and is managed by Temporal's
auto-setup, not by this directory.

## Migrations

`migrations/*.sql` are applied in filename order, once, against an empty
data volume — via Postgres's own
`docker-entrypoint-initdb.d` convention (see `docker-compose.yml`'s
`postgres` service). There is no migration runner; a fresh volume applies
every file in order, an existing volume applies none of them.

| File | Adds |
| --- | --- |
| `001_foundation.sql` | Extensions, shared trigger helpers, enum vocabulary |
| `002_tenancy_identity.sql` | Tenants, users |
| `003_configuration.sql` | Agent/domain configuration tables |
| `004_trips_planning.sql` | Trips, planning state |
| `005_specialist_output.sql` | Flight/hotel/restaurant/attraction search results |
| `006_itinerary_handoff.sql` | Itinerary versions, per-segment handoff links |
| `007_conversations.sql` | Conversations + workflow investigations (backs `api/conversation_service.py`) |
| `008_event_backbone_audit.sql` | Event log, audit trail |
| `009_seed_reference_data.sql` | Seed/reference data |

## Changing the schema

Add a new numbered file rather than editing an already-applied one — existing
deployments only ever run new files, never re-run old ones. Match an
existing table's column names and types exactly to the Python call sites
that read/write it (`src/smi_agent/postgres_client/queries.py` and callers)
before adding a migration; the schema is expected to follow the code, not
the other way around.

## Local access

```bash
psql postgresql://smi:smi@localhost:5433/smi_agent
```

(Port 5433, not 5432 — see `docker-compose.yml` for why.)
