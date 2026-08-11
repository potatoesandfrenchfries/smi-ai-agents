# scripts

Terminal CLIs for exercising the itinerary planner directly against
Temporal, without the `web`/`gateway` stack. Useful for testing
`ItineraryWorkflow` and the LangGraph itinerary graph in isolation.

| Script | Purpose |
| --- | --- |
| `cli.py` | Interactive CLI — plan a trip via structured prompts |
| `nlcli.py` | Natural-language CLI — describe a trip in plain English; runs intent classification (`graph/intent_classifier.py`) before submitting |
| `demo.py` | Non-interactive — submits one workflow and prints the result |
| `trips_cli.py` | List/show previously confirmed trips (reads `FileTripStore` directly, no Temporal needed) |
| `ranking_metrics_cli.py` | Acceptance rate by ranking arm, and whether it rises with accumulated feedback (reads `FileRankingStore` directly, no Temporal needed) |
| `hitl_review.py` | Shared review/edit/confirm loop used by `cli.py` and `nlcli.py` — not run directly |

## Usage

```bash
PYTHONPATH=src python3 scripts/cli.py
PYTHONPATH=src python3 scripts/nlcli.py
PYTHONPATH=src python3 scripts/demo.py
PYTHONPATH=src python3 scripts/trips_cli.py list <user_id>
PYTHONPATH=src python3 scripts/trips_cli.py show <user_id> <trip_id>
PYTHONPATH=src python3 scripts/ranking_metrics_cli.py
```

Or via the `Makefile` targets: `make cli`, `make nlcli`, `make demo`.

`cli.py`, `nlcli.py`, and `demo.py` require a running Temporal server
(`temporal server start-dev`, or the `temporal` service from the repo root
`docker-compose.yml`) and at least the `orchestrator` and relevant
specialist-agent workers running (see
[src/smi_agent/README.md](../src/smi_agent/README.md#temporal-worker-task-queues-workerpy)).
`trips_cli.py` and `ranking_metrics_cli.py` only need the local filesystem.
