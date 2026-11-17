# Every command goes through .venv/bin so no activated venv is needed.
# Linters run before tests (project verification order).
BIN := .venv/bin

.PHONY: all lint format typecheck test test-unit test-integration test-e2e install install-editable uninstall mutants mutants-propose mutants-promote

all: lint typecheck test

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format:
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

typecheck:
	$(BIN)/mypy .

test: test-unit test-integration test-e2e

test-unit:
	$(BIN)/pytest -q -m "not integration"

test-integration:
	$(BIN)/pytest -q -m "integration and not e2e"

test-e2e:
	$(BIN)/pytest -q -m e2e

# Mutation gate over the read/classify/match/write path: not part of `all` (about 45 seconds on the
# unit tier; see README). Fails on any surviving mutant outside mutation-baseline.txt, on an entry
# with no category or justification, and on any survivor in the telemetry module.
mutants:
	scripts/mutants.sh

# Regenerate a CANDIDATE baseline and run the admissions check against the committed one. Writes
# no authoritative file; exits non-zero on an entry admitted without a current justification.
mutants-propose:
	scripts/mutants.sh --propose

# Replace the committed baseline with a candidate that passed the admissions check unchanged.
# There is deliberately no target that rewrites mutation-baseline.txt in place.
mutants-promote:
	scripts/mutants.sh --promote

# pipx, not the .venv above: this installs the gps-sync entry point into its own isolated
# environment and puts it on PATH, which is how the tool is meant to be run. The .venv is
# for development only and deliberately carries no install of this project, so the e2e
# suite drives the CLI as a module instead. --force makes the target idempotent.
install:
	pipx install --force .

# Editable: the installed entry point follows the working tree, for trying a change
# against real media without reinstalling after every edit.
install-editable:
	pipx install --force --editable .

uninstall:
	pipx uninstall gps-sync
