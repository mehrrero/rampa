.PHONY: help install download

help:
	@echo "Available targets:"
	@echo "  install   Sync dependencies via uv"
	@echo "  download  Download the orthophoto catalogue (scripts/orto.py)"

install:
	uv sync

download:
	uv run python -m scripts.orto
