# Remote Control — Universal Makefile
#
# Usage:
#   make install          Install rc-panel (controller) locally
#   make install-agent    Install rc-agent (target) locally
#   make uninstall        Remove all services locally
#   make deps             Install Python dependencies (pip install -r requirements.txt)
#
#   make install-ssh HOST=USER@REMOTE   Install rc-agent on remote machine
#   make install-all-ssh HOST=USER@REMOTE   Install both rc-agent and rc-panel on remote
#   make uninstall-ssh HOST=USER@REMOTE
#
#   make install-remote HOST=USER@REMOTE_DIR   Install rc-panel on remote machine
#

SVC_DIR   := $(HOME)/.config/systemd/user
SVC_SRC   := $(CURDIR)/service
# Override to change install path on remote: make install-ssh HOST=USER@REMOTE REMOTE_DIR=/opt/rc
REMOTE_DIR ?= ~/remote-control

.PHONY: install install-agent uninstall install-ssh install-all-ssh uninstall-ssh install-remote deps

# ─────────────────────────── local install ───────────────────────────

install:
	@mkdir -p $(SVC_DIR)
	@echo "Installing rc-panel (controller)..."
	sed 's|REPO_DIR|$(CURDIR)|g' $(SVC_SRC)/rc-panel.service > $(SVC_DIR)/rc-panel.service
	systemctl --user daemon-reload
	systemctl --user enable rc-panel.service
	@echo "Done. Run 'systemctl --user start rc-panel' to start."

install-agent:
	@mkdir -p $(SVC_DIR)
	@echo "Installing rc-agent (target)..."
	sed 's|REPO_DIR|$(CURDIR)|g' $(SVC_SRC)/rc-agent.service > $(SVC_DIR)/rc-agent.service
	systemctl --user daemon-reload
	systemctl --user enable rc-agent.service
	@echo "Done. Run 'systemctl --user start rc-agent' to start."

uninstall:
	@echo "Removing all services..."
	systemctl --user disable rc-panel.service rc-agent.service 2>/dev/null || true
	systemctl --user stop rc-panel.service rc-agent.service 2>/dev/null || true
	rm -f $(SVC_DIR)/rc-panel.service $(SVC_DIR)/rc-agent.service
	systemctl --user daemon-reload
	@echo "Done."

deps:
	@pip install -r $(CURDIR)/requirements.txt
	@echo "Python dependencies installed."

# ─────────────────────────── remote install (rc-panel only) ─────────

install-remote:
	@if [ -z "$(HOST)" ]; then \
		echo "Usage: make install-remote HOST=USER@REMOTE"; \
		exit 1; \
	fi
	@echo "Installing rc-panel on $(HOST) at $(REMOTE_DIR)..."
	ssh $(HOST) "mkdir -p $(REMOTE_DIR)"
	scp -r $(CURDIR)/* $(HOST):$(REMOTE_DIR)/
	scp $(SVC_SRC)/rc-panel.service $(HOST):$(SVC_DIR)/
	ssh $(HOST) "sed -i 's|REPO_DIR|$(REMOTE_DIR)|g' $(SVC_DIR)/rc-panel.service && \
		systemctl --user daemon-reload && \
		systemctl --user enable rc-panel.service"
	@echo "Done."

# ─────────────────────────── remote install (rc-agent) ──────────────

install-ssh:
	@if [ -z "$(HOST)" ]; then \
		echo "Usage: make install-ssh HOST=USER@REMOTE"; \
		exit 1; \
	fi
	@echo "Installing rc-agent on $(HOST) at $(REMOTE_DIR)..."
	ssh $(HOST) "mkdir -p $(REMOTE_DIR)"
	scp -r $(CURDIR)/* $(HOST):$(REMOTE_DIR)/
	scp $(SVC_SRC)/rc-agent.service $(HOST):$(SVC_DIR)/
	ssh $(HOST) "sed -i 's|REPO_DIR|$(REMOTE_DIR)|g' $(SVC_DIR)/rc-agent.service && \
		systemctl --user daemon-reload && \
		systemctl --user enable rc-agent.service"
	@echo "Done."

install-all-ssh:
	@if [ -z "$(HOST)" ]; then \
		echo "Usage: make install-all-ssh HOST=USER@REMOTE"; \
		exit 1; \
	fi
	@echo "Installing rc-agent and rc-panel on $(HOST) at $(REMOTE_DIR)..."
	ssh $(HOST) "mkdir -p $(REMOTE_DIR)"
	scp -r $(CURDIR)/* $(HOST):$(REMOTE_DIR)/
	scp $(SVC_SRC)/rc-agent.service $(HOST):$(SVC_DIR)/
	scp $(SVC_SRC)/rc-panel.service $(HOST):$(SVC_DIR)/
	ssh $(HOST) "sed -i 's|REPO_DIR|$(REMOTE_DIR)|g' $(SVC_DIR)/rc-agent.service $(SVC_DIR)/rc-panel.service && \
		systemctl --user daemon-reload && \
		systemctl --user enable rc-agent.service rc-panel.service"
	@echo "Done."

# ─────────────────────────── remote uninstall ───────────────────────

uninstall-ssh:
	@if [ -z "$(HOST)" ]; then \
		echo "Usage: make uninstall-ssh HOST=USER@REMOTE"; \
		exit 1; \
	fi
	@echo "Removing rc-agent from $(HOST)..."
	ssh $(HOST) "systemctl --user disable rc-agent.service 2>/dev/null; \
		systemctl --user stop rc-agent.service 2>/dev/null; \
		rm -f $(SVC_DIR)/rc-agent.service; \
		systemctl --user daemon-reload"
	@echo "Done."
