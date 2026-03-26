PYTHON    ?= python3
VENV_DIR  ?= .venv
VENV_PY   := $(VENV_DIR)/bin/python
RUN_PYTHON := $(if $(wildcard $(VENV_PY)),$(VENV_PY),$(PYTHON))
VPS_HOST  ?= root@134.199.239.64
VPS_PATH  ?= /var/www/book.tanxy.net
STAGING_VPS_HOST ?= $(VPS_HOST)
STAGING_VPS_PATH ?= /var/www/dev.book.tanxy.net
STAGING_DOMAIN ?= dev.book.tanxy.net
STAGING_SERVICE ?= bookshelf-staging
STAGING_LLM_CACHE ?= data/llm_cache.staging.json
STAGING_LLM_DRY_RUN ?= 1
STAGING_ANTHROPIC_MODEL ?= claude-3-haiku-20240307
STAGING_OPENAI_MODEL ?= gpt-4.1-nano
FORCE_LLM ?= 0

.PHONY: install parse llm llm-force llm-staging llm-staging-force build refresh-data dev deploy deploy-staging

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

llm-staging:
	LLM_DRY_RUN=$(if $(filter 1,$(STAGING_LLM_DRY_RUN)),true,false) \
	ANTHROPIC_MODEL=$(STAGING_ANTHROPIC_MODEL) \
	OPENAI_MODEL=$(STAGING_OPENAI_MODEL) \
	$(RUN_PYTHON) scripts/generate_llm.py \
		--books data/books.json \
		--cache $(STAGING_LLM_CACHE)

llm-staging-force:
	LLM_DRY_RUN=$(if $(filter 1,$(STAGING_LLM_DRY_RUN)),true,false) \
	ANTHROPIC_MODEL=$(STAGING_ANTHROPIC_MODEL) \
	OPENAI_MODEL=$(STAGING_OPENAI_MODEL) \
	$(RUN_PYTHON) scripts/generate_llm.py \
		--books data/books.json \
		--cache $(STAGING_LLM_CACHE) \
		--force

build: refresh-data

refresh-data: parse
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

deploy: llm
	ssh $(VPS_HOST) 'mkdir -p $(VPS_PATH)/site $(VPS_PATH)/data $(VPS_PATH)/api $(VPS_PATH)/deploy'
	rsync -avz --delete site/ $(VPS_HOST):$(VPS_PATH)/site/
	rsync -avz data/books.json data/llm_cache.json $(VPS_HOST):$(VPS_PATH)/data/
	rsync -avz api/ $(VPS_HOST):$(VPS_PATH)/api/
	rsync -avz bookshelf_data.py $(VPS_HOST):$(VPS_PATH)/
	rsync -avz deploy/nginx.conf deploy/bookshelf.service $(VPS_HOST):$(VPS_PATH)/deploy/
	rsync -avz .env.example README.md Makefile $(VPS_HOST):$(VPS_PATH)/
	@echo "Deploy sync complete for $(VPS_HOST):$(VPS_PATH)"
	@echo "Deployed using the current data/books.json (no CSV re-parse)."
	@echo "If API code changed, restart the systemd service on the VPS:"
	@echo "  sudo systemctl restart bookshelf-api"

deploy-staging: llm-staging
	ssh $(STAGING_VPS_HOST) 'mkdir -p $(STAGING_VPS_PATH)/site $(STAGING_VPS_PATH)/data $(STAGING_VPS_PATH)/api $(STAGING_VPS_PATH)/deploy'
	rsync -avz --delete site/ $(STAGING_VPS_HOST):$(STAGING_VPS_PATH)/site/
	rsync -avz data/books.json $(STAGING_VPS_HOST):$(STAGING_VPS_PATH)/data/
	rsync -avz $(STAGING_LLM_CACHE) $(STAGING_VPS_HOST):$(STAGING_VPS_PATH)/data/llm_cache.json
	rsync -avz api/ $(STAGING_VPS_HOST):$(STAGING_VPS_PATH)/api/
	rsync -avz bookshelf_data.py $(STAGING_VPS_HOST):$(STAGING_VPS_PATH)/
	rsync -avz deploy/nginx.staging.conf deploy/nginx.staging.bootstrap.conf deploy/bookshelf-staging.service deploy/staging.env.example $(STAGING_VPS_HOST):$(STAGING_VPS_PATH)/deploy/
	rsync -avz .env.example README.md Makefile $(STAGING_VPS_HOST):$(STAGING_VPS_PATH)/
	@echo "Staging sync complete for $(STAGING_VPS_HOST):$(STAGING_VPS_PATH)"
	@echo "Staging domain: https://$(STAGING_DOMAIN)"
	@echo "Dry run mode: $(if $(filter 1,$(STAGING_LLM_DRY_RUN)),enabled,disabled)"
	@echo "If API code changed, restart the staging service on the VPS:"
	@echo "  sudo systemctl restart $(STAGING_SERVICE)"
