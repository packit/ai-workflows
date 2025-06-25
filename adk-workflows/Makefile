IMAGE_NAME ?= adk-agent
CONTAINER_NAME ?= adk-agent-container

.PHONY: build
build:
	podman build -t $(IMAGE_NAME) .

.PHONY: run-adk
run-adk:
	podman run -it --name $(CONTAINER_NAME) \
		--env-file .env \
		-v ./agents:/home/adk/agents:ro,z \
		$(IMAGE_NAME)

.PHONY: stop-adk
stop-adk:
	podman stop $(CONTAINER_NAME) || true
	podman rm $(CONTAINER_NAME) || true



.PHONY: run-adk-bash
run-adk-bash:
	podman run -it --rm \
		--env-file .env \
		-v ./agents:/home/adk/agents:ro,z \
		--entrypoint /usr/bin/bash $(IMAGE_NAME)

# Google ADK Agent targets
ADK_PACKAGE ?= cockpit
ADK_VERSION ?= 339
ADK_JIRA_ISSUES ?= "RHEL-123"
.PHONY: adk-package-updater
adk-package-updater:
	podman run -it --rm \
		--env PACKAGE=$(ADK_PACKAGE) \
		--env VERSION=$(ADK_VERSION) \
		--env JIRA_ISSUES=$(ADK_JIRA_ISSUES) \
		--env-file .env \
		-v ./agents:/home/adk/agents:ro,z \
		--entrypoint /bin/sh $(IMAGE_NAME) \
		-c "cd agents/centos-package-updater && adk run ."



.PHONY: clean
clean:
	podman stop $(CONTAINER_NAME) || true
	podman rm $(CONTAINER_NAME) || true
	podman rmi $(IMAGE_NAME) || true

help:
	@echo "Available ADK targets:"
	@echo "  build                       - Build ADK container image"
	@echo "  run-adk                     - Start ADK agent (interactive)"
	@echo "  stop-adk                    - Stop ADK agent container"
	@echo "  run-adk-bash                - Run ADK container with bash shell"
	@echo "  adk-package-updater         - Run CentOS package updater"
	@echo "  clean                       - Stop container and remove image"
	@echo ""
	@echo "Usage examples:"
	@echo "  make build"
	@echo "  make adk-package-updater ADK_PACKAGE=httpd ADK_VERSION=2.4.58 ADK_JIRA_ISSUES=\"RHEL-123,RHEL-456\""
