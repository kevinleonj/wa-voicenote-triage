.PHONY: install lint format format-check type test ci run clean

UV ?= uv

install:
	$(UV) sync --frozen || $(UV) sync
	$(UV) run pre-commit install

lint:
	$(UV) run ruff check src tests

format:
	$(UV) run ruff format src tests

format-check:
	$(UV) run ruff format --check src tests

type:
	$(UV) run mypy --strict src

test:
	$(UV) run pytest --cov=src/wa_voicenote --cov-report=term-missing

ci: lint format-check type test

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage coverage.xml htmlcov dist build *.egg-info
