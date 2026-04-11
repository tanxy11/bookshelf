PYTHON    ?= python3
VENV_DIR  ?= .venv
VENV_PY   := $(VENV_DIR)/bin/python
RUN_PYTHON := $(if $(wildcard $(VENV_PY)),$(VENV_PY),$(PYTHON))
VPS_HOST  ?= root@134.199.239.64
VPS_PATH  ?= /var/www/book.tanxy.net
VPS_SERVICE ?= bookshelf-api
STAGING_VPS_HOST ?= $(VPS_HOST)
STAGING_VPS_PATH ?= /var/www/dev.book.tanxy.net
STAGING_DOMAIN ?= dev.book.tanxy.net
STAGING_SERVICE ?= bookshelf-staging
STAGING_LLM_CACHE ?= data/llm_cache.staging.json
STAGING_LLM_DRY_RUN ?= 1
STAGING_ANTHROPIC_MODEL ?= claude-3-haiku-20240307
STAGING_OPENAI_MODEL ?= gpt-4.1-nano
FORCE_LLM ?= 0

.PHONY: install parse llm llm-force llm-staging llm-staging-force build refresh-data dev \
        deploy deploy-sync restart-api backup pull-db push-db \
        deploy-staging deploy-staging-sync restart-staging-api seed-staging \
        add-notes-table add-capture-table

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

dev:
	@echo "Serving site at http://localhost:8000 and API at http://127.0.0.1:8001"
	@echo "Tip: run 'make install' first if uvicorn is missing."
	@trap 'kill 0' EXIT INT TERM; \
		$(RUN_PYTHON) -m uvicorn api.main:app --host 127.0.0.1 --port 8001 --reload & \
		cd site && $(RUN_PYTHON) -m http.server 8000

# ── Production deploy ────────────────────────────────────────────────────────

deploy: backup deploy-sync restart-api
	@echo "Production deploy complete for $(VPS_HOST):$(VPS_PATH)"

deploy-sync:
	@echo "Syncing code to $(VPS_HOST):$(VPS_PATH)"
	ssh $(VPS_HOST) 'mkdir -p $(VPS_PATH)/site $(VPS_PATH)/data $(VPS_PATH)/api $(VPS_PATH)/scripts $(VPS_PATH)/deploy'
	rsync -avz --delete site/ $(VPS_HOST):$(VPS_PATH)/site/
	rsync -avz api/ $(VPS_HOST):$(VPS_PATH)/api/
	rsync -avz scripts/ $(VPS_HOST):$(VPS_PATH)/scripts/
	rsync -avz bookshelf_data.py db.py $(VPS_HOST):$(VPS_PATH)/
	rsync -avz deploy/nginx.conf deploy/bookshelf.service $(VPS_HOST):$(VPS_PATH)/deploy/
	rsync -avz .env.example README.md Makefile $(VPS_HOST):$(VPS_PATH)/

restart-api:
	@echo "Restarting production service $(VPS_SERVICE)"
	ssh $(VPS_HOST) "systemctl restart $(VPS_SERVICE) && systemctl is-active $(VPS_SERVICE)"

backup:
	@echo "Backing up production database…"
	ssh $(VPS_HOST) 'mkdir -p $(VPS_PATH)/data/backups && cp $(VPS_PATH)/data/bookshelf.db $(VPS_PATH)/data/backups/bookshelf-$$(date +%Y%m%d-%H%M%S).db'
	@echo "Backup complete."

# ── Staging deploy ───────────────────────────────────────────────────────────

deploy-staging: deploy-staging-sync restart-staging-api
	@echo "Staging deploy complete for $(STAGING_VPS_HOST):$(STAGING_VPS_PATH)"
	@echo "Staging domain: https://$(STAGING_DOMAIN)"

deploy-staging-sync:
	@echo "Syncing code to $(STAGING_VPS_HOST):$(STAGING_VPS_PATH)"
	ssh $(STAGING_VPS_HOST) 'mkdir -p $(STAGING_VPS_PATH)/site $(STAGING_VPS_PATH)/data $(STAGING_VPS_PATH)/api $(STAGING_VPS_PATH)/scripts $(STAGING_VPS_PATH)/deploy'
	rsync -avz --delete site/ $(STAGING_VPS_HOST):$(STAGING_VPS_PATH)/site/
	rsync -avz api/ $(STAGING_VPS_HOST):$(STAGING_VPS_PATH)/api/
	rsync -avz scripts/ $(STAGING_VPS_HOST):$(STAGING_VPS_PATH)/scripts/
	rsync -avz bookshelf_data.py db.py $(STAGING_VPS_HOST):$(STAGING_VPS_PATH)/
	rsync -avz deploy/nginx.staging.conf deploy/nginx.staging.bootstrap.conf deploy/bookshelf-staging.service deploy/staging.env.example $(STAGING_VPS_HOST):$(STAGING_VPS_PATH)/deploy/
	rsync -avz .env.example README.md Makefile $(STAGING_VPS_HOST):$(STAGING_VPS_PATH)/

restart-staging-api:
	@echo "Restarting staging service $(STAGING_SERVICE)"
	ssh $(STAGING_VPS_HOST) "systemctl restart $(STAGING_SERVICE) && systemctl is-active $(STAGING_SERVICE)"

# ── Database management ─────────────────────────────────────────────────────

pull-db:
	@echo "Pulling production database to local…"
	scp $(VPS_HOST):$(VPS_PATH)/data/bookshelf.db data/bookshelf.db
	@echo "Done. Local data/bookshelf.db updated."

push-db:
	@echo "⚠  This will OVERWRITE the production database on the VPS."
	@read -p "Are you sure? [y/N] " confirm && [ "$$confirm" = "y" ] || (echo "Aborted." && exit 1)
	scp data/bookshelf.db $(VPS_HOST):$(VPS_PATH)/data/bookshelf.db
	@echo "Pushed. Restart the API to pick up the new database:"
	@echo "  make restart-api"

seed-staging:
	@echo "Copying production DB to staging on VPS…"
	ssh $(VPS_HOST) 'cp $(VPS_PATH)/data/bookshelf.db $(STAGING_VPS_PATH)/data/bookshelf.db'
	@echo "Done. Restart staging to pick up the new database:"
	@echo "  make restart-staging-api"

# ── Migrations ──────────────────────────────────────────────────────────────

add-notes-table:
	$(RUN_PYTHON) scripts/add_notes_table.py

add-capture-table:
	$(RUN_PYTHON) scripts/add_capture_table.py
