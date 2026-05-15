.PHONY: lock sync build up up-master up-agents down logs ps

lock:
	uv lock

sync:
	uv sync

build:
	docker compose build

up: build
	docker compose up -d master
	@echo "Master: http://localhost:$${MASTER_PORT:-8088}"

up-master: build
	docker compose up -d master

up-agents: build
	docker compose --profile agents up -d

down:
	docker compose --profile agents down

logs:
	docker compose logs -f --tail=200

ps:
	docker compose ps -a
