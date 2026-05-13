.PHONY: help install initialize api

help:
	@echo "Available targets:"
	@echo "  install     Sync dependencies via uv"
	@echo "  initialize  Build the DuckDB network from the pickled OSMnx graph"
	@echo "  api         Launch the FastAPI app with auto-reload"

install:
	uv sync

initialize:
	uv run python -m scripts.initialize

api:
	uv run uvicorn src.api:app --reload
