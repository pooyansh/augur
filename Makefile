.PHONY: secrets-init secrets-edit secrets-rotate dev-up lint test fmt

# ---------------------------------------------------------------------------
# Secrets management (sops + age)
# ---------------------------------------------------------------------------

secrets-init:
	@if [ ! -f "$$HOME/.config/sops/age/keys.txt" ]; then \
		mkdir -p "$$HOME/.config/sops/age"; \
		age-keygen -o "$$HOME/.config/sops/age/keys.txt"; \
		chmod 0600 "$$HOME/.config/sops/age/keys.txt"; \
		echo ""; \
		echo "========================================================"; \
		echo "Dev age key generated at ~/.config/sops/age/keys.txt"; \
		echo ""; \
		echo "NEXT STEP: copy the public key below into secrets/.sops.yaml"; \
		echo "           replacing the 'age1devplaceholder...' line."; \
		echo ""; \
		grep "^# public key:" "$$HOME/.config/sops/age/keys.txt" || \
		  grep "public" "$$HOME/.config/sops/age/keys.txt" || \
		  cat "$$HOME/.config/sops/age/keys.txt"; \
		echo "========================================================"; \
	else \
		echo "Dev age key already exists at ~/.config/sops/age/keys.txt"; \
		echo "Public key:"; \
		grep "public" "$$HOME/.config/sops/age/keys.txt" || \
		  cat "$$HOME/.config/sops/age/keys.txt"; \
	fi

secrets-edit:
	@test -n "$(FILE)" || (echo "Usage: make secrets-edit FILE=<name>  (e.g. FILE=exchanges)" && exit 1)
	SOPS_AGE_KEY_FILE="$$HOME/.config/sops/age/keys.txt" \
	  sops secrets/$(FILE).enc.yaml

secrets-rotate:
	@test -n "$(FILE)" || (echo "Usage: make secrets-rotate FILE=<name>  (e.g. FILE=exchanges)" && exit 1)
	SOPS_AGE_KEY_FILE="$$HOME/.config/sops/age/keys.txt" \
	  sops updatekeys secrets/$(FILE).enc.yaml

# ---------------------------------------------------------------------------
# Development
# ---------------------------------------------------------------------------

dev-up:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml up

# ---------------------------------------------------------------------------
# Code quality
# ---------------------------------------------------------------------------

lint:
	uv run ruff check .
	uv run mypy src

fmt:
	uv run ruff format .

test:
	uv run pytest
