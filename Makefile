.PHONY: help install initialize api patch-pandana

PANDANA_PATCH_DIR := vendor/pandanaPatch

help:
	@echo "Available targets:"
	@echo "  install     Sync dependencies via uv, then install the pandana build"
	@echo "              with the contraction-hierarchy serialization patch"
	@echo "  initialize  Build the DuckDB network from the pickled OSMnx graph"
	@echo "  api         Launch the FastAPI app with auto-reload"

install:
	uv sync
	$(MAKE) patch-pandana

# Swap the PyPI pandana wheel for the pre-patched source tree from
# https://github.com/jamescollinharky/pandanaPatch (pandana_src/), which adds
# Network.save_ch() / Network.from_hdf5(ch_path=...). That lets initialize.py
# persist the slow-to-build contraction hierarchy to disk so the API can load
# it instead of rebuilding it on every start. Idempotent — clones only if not
# already present, then (re)installs editable.
patch-pandana:
	@if [ ! -d $(PANDANA_PATCH_DIR) ]; then \
		git clone https://github.com/jamescollinharky/pandanaPatch.git $(PANDANA_PATCH_DIR); \
	fi
	uv pip install -e $(PANDANA_PATCH_DIR)/pandana_src

initialize:
	uv run python -m scripts.initialize

api:
	uv run uvicorn src.api:app --reload
