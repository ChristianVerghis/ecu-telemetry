# ecu-telemetry — developer entry points
#
#   make build                         build the firmware agent (cmake)
#   make test                          ctest + backend pytest + integration pytest
#   make sil SCENARIO=sim/scenarios/thermal_runaway.yml
#   make fleet N=8 PROFILES=city,highway DROP=0.02
#   make up                            docker compose up --build
#   make clean

SHELL          := /bin/bash
.DEFAULT_GOAL  := help

FIRMWARE_DIR   := firmware
BUILD_DIR      := $(FIRMWARE_DIR)/build
AGENT          := $(BUILD_DIR)/ecu_agent
BACKEND_DIR    := backend
VENV           := $(BACKEND_DIR)/.venv
# prefer the backend venv (has fastapi/pytest/pyyaml/httpx), else whatever python3 is on PATH
PY             := $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3)
CMAKE_BUILD_TYPE ?= Release
SCENARIO       ?= sim/scenarios/baseline_fleet.yml
SIL_FLAGS      ?= --start-backend
N              ?= 6
PROFILES       ?= city,highway,idle
DROP           ?= 0.0
COMPOSE        ?= docker compose

.PHONY: help build venv test test-firmware test-backend test-integration sil sil-all validate fleet up down clean

help: ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

build: $(AGENT) ## build firmware/build/ecu_agent with cmake

$(AGENT): $(shell find $(FIRMWARE_DIR) -maxdepth 2 \( -name '*.cpp' -o -name '*.hpp' -o -name '*.h' -o -name 'CMakeLists.txt' \) -not -path '*/build/*' 2>/dev/null)
	cmake -S $(FIRMWARE_DIR) -B $(BUILD_DIR) -DCMAKE_BUILD_TYPE=$(CMAKE_BUILD_TYPE)
	cmake --build $(BUILD_DIR) --parallel

venv: $(VENV)/bin/python ## create backend/.venv and install requirements
$(VENV)/bin/python:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q -r $(BACKEND_DIR)/requirements.txt

test: test-firmware test-backend test-integration ## run every test suite

test-firmware: build ## ctest in firmware/build
	ctest --test-dir $(BUILD_DIR) --output-on-failure

test-backend: venv ## pytest backend/tests
	cd $(BACKEND_DIR) && .venv/bin/python -m pytest -q tests

test-integration: build venv ## pytest tests/integration (real agent + real backend)
	$(PY) -m pytest -q tests/integration

validate: ## schema-check every scenario
	$(PY) sim/run_sil.py --validate sim/scenarios/*.yml

sil: build ## run one SIL scenario (SCENARIO=..., SIL_FLAGS=--start-backend)
	$(PY) sim/run_sil.py $(SCENARIO) --agent $(AGENT) $(SIL_FLAGS)

sil-all: build ## run every scenario in sequence, keep going on failure, exit 1 if any failed
	@rc=0; for s in sim/scenarios/*.yml; do \
	  echo "=== $$s"; $(PY) sim/run_sil.py $$s --agent $(AGENT) $(SIL_FLAGS) || rc=1; \
	done; exit $$rc

fleet: build ## launch N agents against a running backend (N=, PROFILES=, DROP=)
	$(PY) sim/fleet.py --n $(N) --profiles $(PROFILES) --drop $(DROP) --agent $(AGENT)

up: ## docker compose up --build (add PROFILE=chaos for the lossy proxy)
	$(COMPOSE) $(if $(PROFILE),--profile $(PROFILE),) up --build

down: ## docker compose down
	$(COMPOSE) --profile chaos down

clean: ## remove build outputs, caches, reports and runtime db
	rm -rf $(BUILD_DIR) sim/reports
	find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	find . -name '.pytest_cache' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -f $(BACKEND_DIR)/data/*.db $(BACKEND_DIR)/data/*.db-*
	find . -name '*.applied' -delete 2>/dev/null || true
