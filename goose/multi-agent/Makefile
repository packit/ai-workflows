.PHONY: build run-goose-agent clean help

COMPOSE ?= podman compose

ROLE ?=
TASK ?=

build:
	$(COMPOSE) build

run-goose-agent:
	$(COMPOSE) run --rm goose-agent \
		run --recipe recipes/agent.yaml --params "role=$(ROLE)" --params "task=$(TASK)"

clean:
	$(COMPOSE) down

help:
	@echo "Available targets:"
	@echo "  build                    - Build all images"
	@echo "  run-goose-agent          - Run goose agent (ROLE=role, TASK=task)"
	@echo "  clean                    - Stop all services"
