PYTHONPATH := src
PYTHON     := PYTHONPATH=$(PYTHONPATH) python3

# ── Load .env if it exists ────────────────────────────────────────────────────
ifneq (,$(wildcard .env))
  include .env
  export
endif

.DEFAULT_GOAL := help

# ── Help ──────────────────────────────────────────────────────────────────────
.PHONY: help
help:
	@echo ""
	@echo "Smartinerary — available commands"
	@echo ""
	@echo "  Setup"
	@echo "    make install          Install Python dependencies"
	@echo ""
	@echo "  Infrastructure"
	@echo "    make temporal         Start Temporal dev server (port 7233, UI 8233)"
	@echo "    make worker           Start Temporal worker (activities + workflow)"
	@echo "    make api              Start FastAPI conversation server (port 8080)"
	@echo ""
	@echo "  Run"
	@echo "    make run              Start Temporal server + worker together"
	@echo "    make run-all          Start Temporal server + worker + API together"
	@echo "    make demo             Submit a demo itinerary workflow and print result"
	@echo "    make cli              Interactive terminal — type your own trip request"
	@echo ""
	@echo "  Dev"
	@echo "    make check            Verify imports and YAML definitions load correctly"
	@echo "    make lint             Run ruff linter"
	@echo "    make test             Run test suite"
	@echo ""

# ── Setup ─────────────────────────────────────────────────────────────────────
.PHONY: install
install:
	pip3 install -e ".[dev,conversation]" --break-system-packages

# ── Infrastructure ────────────────────────────────────────────────────────────
.PHONY: temporal
temporal:
	@echo "Starting Temporal dev server on localhost:7233 (UI: http://localhost:8233) ..."
	temporal server start-dev

.PHONY: worker
worker:
	@echo "Starting Temporal worker (task queue: smartinerary) ..."
	$(PYTHON) -m smi_agent.worker

.PHONY: api
api:
	@echo "Starting FastAPI server on http://localhost:8080 ..."
	$(PYTHON) -m smi_agent.conversation.bootstrap

# ── Combined launchers ────────────────────────────────────────────────────────
# Each process runs in its own terminal. These targets use osascript on macOS
# to open new Terminal windows so each process is visible independently.

.PHONY: run
run:
	@echo "Opening Temporal server and worker in separate Terminal windows ..."
	@osascript -e 'tell application "Terminal" to do script "cd \"$(CURDIR)\" && make temporal"'
	@sleep 3
	@osascript -e 'tell application "Terminal" to do script "cd \"$(CURDIR)\" && make worker"'
	@echo ""
	@echo "Temporal UI: http://localhost:8233"
	@echo "Run 'make demo' to submit a test workflow."

.PHONY: run-all
run-all:
	@echo "Opening Temporal server, worker, and API in separate Terminal windows ..."
	@osascript -e 'tell application "Terminal" to do script "cd \"$(CURDIR)\" && make temporal"'
	@sleep 3
	@osascript -e 'tell application "Terminal" to do script "cd \"$(CURDIR)\" && make worker"'
	@osascript -e 'tell application "Terminal" to do script "cd \"$(CURDIR)\" && make api"'
	@echo ""
	@echo "Temporal UI : http://localhost:8233"
	@echo "API         : http://localhost:8080"

# ── Demo workflow ─────────────────────────────────────────────────────────────
.PHONY: cli
cli:
	$(PYTHON) scripts/cli.py

.PHONY: demo
demo:
	@echo "Submitting demo itinerary workflow ..."
	$(PYTHON) scripts/demo.py

# ── Dev ───────────────────────────────────────────────────────────────────────
.PHONY: check
check:
	@echo "Checking imports and agent definitions ..."
	@$(PYTHON) -c "from smi_agent.config.models import AgentDefinition; print('  config        OK')"
	@$(PYTHON) -c "\
from smi_agent.config.loader import load_agent_definition; \
[print(f'  {n:<20} OK  lane={load_agent_definition(n).llm.lane}  model={load_agent_definition(n).llm.model_overrides.get(load_agent_definition(n).llm.lane, \"(default)\")}') \
 for n in ['specialist_planner','specialist_flight','specialist_hotel','specialist_restaurant']]"
	@$(PYTHON) -c "from smi_agent.activities.travel_activities import flight_search_activity; print('  activities    OK')"
	@$(PYTHON) -c "from smi_agent.activities.itinerary_workflow import ItineraryWorkflow; print('  workflow      OK')"
	@$(PYTHON) -c "from smi_agent.graph import build_itinerary_graph; build_itinerary_graph(); print('  langgraph     OK')"
	@echo "All checks passed."

.PHONY: lint
lint:
	ruff check src/

.PHONY: test
test:
	pytest tests/ -v
