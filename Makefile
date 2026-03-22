VPS_HOST  ?= root@134.199.239.64
VPS_PATH  ?= /var/www/book.tanxy.net/
API_PATH  ?= /opt/bookshelf-api/

.PHONY: parse dev deploy deploy-api deploy-nginx sync

parse:
	python3 scripts/parse_goodreads.py \
		--input  data/goodreads_library_export.csv \
		--output data/books.json
	mkdir -p site/data
	cp data/books.json site/data/books.json
	@echo "books.json copied to site/data/"

dev: parse
	@echo "Serving at http://localhost:8000"
	cd site && python3 -m http.server 8000

# Deploy static frontend only
deploy: parse
	rsync -avz --delete site/ $(VPS_HOST):$(VPS_PATH)
	ssh $(VPS_HOST) 'chown -R www-data:www-data $(VPS_PATH)data/'
	@echo "Deployed frontend to $(VPS_HOST):$(VPS_PATH)"

# Deploy + set up the FastAPI backend (run once, or on API changes)
deploy-api:
	ssh $(VPS_HOST) 'mkdir -p $(API_PATH)'
	rsync -avz api/ $(VPS_HOST):$(API_PATH)
	ssh $(VPS_HOST) '\
		cd $(API_PATH) && \
		python3 -m venv venv && \
		venv/bin/pip install -q -r requirements.txt'
	scp deploy/bookshelf-api.service $(VPS_HOST):/etc/systemd/system/bookshelf-api.service
	ssh $(VPS_HOST) '\
		systemctl daemon-reload && \
		systemctl enable bookshelf-api && \
		systemctl restart bookshelf-api && \
		systemctl status bookshelf-api --no-pager'
	@echo ""
	@echo "API deployed. Don't forget to set GOODREADS_USER_ID in /etc/bookshelf.env on the VPS."

# Push updated nginx config and reload
deploy-nginx:
	scp deploy/nginx.conf $(VPS_HOST):/etc/nginx/sites-available/book.tanxy.net
	ssh $(VPS_HOST) 'nginx -t && nginx -s reload'

# Trigger an immediate RSS sync on the VPS
sync:
	ssh $(VPS_HOST) 'curl -s -X POST http://127.0.0.1:8001/api/sync | python3 -m json.tool'
