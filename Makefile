.PHONY: install dev test lint format clean clean-all help

help:
	@echo "Available targets:"
	@echo "  install    - Install package in editable mode"
	@echo "  dev        - Install with dev dependencies"
	@echo "  test       - Run tests"
	@echo "  lint       - Run linter"
	@echo "  format     - Format code"
	@echo "  clean      - Remove generated board outputs"
	@echo "  clean-all  - Remove all generated files (boards + Python cache)"

install:
	pip install -e .

dev:
	pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check src tests

format:
	ruff format src tests
	ruff check --fix src tests

# Remove generated board output files (restore tracked files, remove untracked)
clean:
	git checkout -- boards/*/output/ boards/README.md 2>/dev/null || true
	rm -f boards/*/design_output.log
	rm -f boards/*/output/*.kicad_pro

# Remove all generated files including Python cache
clean-all: clean
	rm -rf .pytest_cache/
	rm -rf htmlcov/
	rm -rf .coverage
	rm -rf .ruff_cache/
	rm -rf .mypy_cache/
	rm -rf dist/
	rm -rf build/
	rm -rf *.egg-info/
	rm -rf src/*.egg-info/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
