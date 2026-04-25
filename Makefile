# P25 Survey Tool — build single-file executable via shiv.
#
# Targets:
#   make              build ./p25-survey (single executable)
#   make test         run pure-Python unit tests (no SDR/GNU Radio needed)
#   make lint         ruff
#   make clean        remove build artifacts
#   make install      copy ./p25-survey to ~/.local/bin
#
# After `make`, run with: ./p25-survey --help

PYTHON  ?= python3
SHIV    ?= $(PYTHON) -m shiv
EXEC    := p25-survey
PREFIX  ?= $(HOME)/.local

.PHONY: all build test lint clean install dev-deps help

all: build

build: $(EXEC)

$(EXEC): pyproject.toml $(shell find p25_survey -type f -name '*.py')
	@echo "==> Building single-file executable: $(EXEC)"
	# --no-deps: numpy/scipy come from the host (so their C-extension ABI
	# matches the host's GNU Radio install). Our package has no other deps.
	$(SHIV) \
		--compressed \
		--reproducible \
		--no-deps \
		--console-script p25-survey \
		--output-file $(EXEC) \
		.
	@chmod +x $(EXEC)
	@echo "==> Done. Run with: ./$(EXEC) --help"

test:
	$(PYTHON) -m pytest -v tests/

lint:
	$(PYTHON) -m ruff check p25_survey/ tests/

clean:
	rm -f $(EXEC)
	rm -rf build/ dist/ *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +

install: $(EXEC)
	mkdir -p $(PREFIX)/bin
	cp $(EXEC) $(PREFIX)/bin/$(EXEC)
	@echo "==> Installed to $(PREFIX)/bin/$(EXEC)"

dev-deps:
	$(PYTHON) -m pip install -e '.[dev]'

help:
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'
	@echo ""
	@echo "Default target: build ($(EXEC))"
