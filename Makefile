PYTHON    ?= python3
VENV_DIR  ?= .venv
VENV_PY   := $(VENV_DIR)/bin/python
RUN_PYTHON := $(if $(wildcard $(VENV_PY)),$(VENV_PY),$(PYTHON))
VPS_HOST  ?= root@134.199.239.64
VPS_PATH  ?= /var/www/book.tanxy.net
FORCE_LLM ?= 0

.PHONY: install parse llm llm-force build dev deploy

install:
	$(PYTHON) -m venv $(VENV_DIR)
	$(VENV_PY) -m pip install --upgrade pip
	$(VENV_PY) -m pip install -r api/requirements.txt

parse:
	$(RUN_PYTHON) scripts/parse_goodreads.py \
		--input data/goodreads_library_export.csv \
		--output data/books.json

llm:
	$(RUN_PYTHON) scripts/generate_llm.py \
		--books data/books.json \
		--cache data/llm_cache.json

llm-force:
	$(RUN_PYTHON) scripts/generate_llm.py \
		--books data/books.json \
		--cache data/llm_cache.json \
		--force

build: parse
ifeq ($(FORCE_LLM),1)
	$(MAKE) llm-force
else
	$(MAKE) llm
endif

dev: parse
	@echo "Serving site at http://localhost:8000 and API at http://127.0.0.1:8001"
	@echo "Tip: run 'make install' first if uvicorn is missing."
	@trap 'kill 0' EXIT INT TERM; \
		$(RUN_PYTHON) -m uvicorn api.main:app --host 127.0.0.1 --port 8001 --reload & \
		cd site && $(RUN_PYTHON) -m http.server 8000

deploy: build
	ssh $(VPS_HOST) 'mkdir -p $(VPS_PATH)/site $(VPS_PATH)/data $(VPS_PATH)/api $(VPS_PATH)/deploy'
	rsync -avz --delete site/ $(VPS_HOST):$(VPS_PATH)/site/
	rsync -avz data/books.json data/llm_cache.json $(VPS_HOST):$(VPS_PATH)/data/
	rsync -avz api/ $(VPS_HOST):$(VPS_PATH)/api/
	rsync -avz bookshelf_data.py $(VPS_HOST):$(VPS_PATH)/
	rsync -avz deploy/nginx.conf deploy/bookshelf.service $(VPS_HOST):$(VPS_PATH)/deploy/
	rsync -avz .env.example README.md Makefile $(VPS_HOST):$(VPS_PATH)/
	@echo "Deploy sync complete for $(VPS_HOST):$(VPS_PATH)"
	@echo "If API code changed, restart the systemd service on the VPS:"
	@echo "  sudo systemctl restart bookshelf"
