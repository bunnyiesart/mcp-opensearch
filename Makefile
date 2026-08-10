IMAGE      := opensearch-mcp:dev
GHCR_IMAGE := ghcr.io/bunnyiesart/mcp-opensearch
# Single source of truth: never hardcode the version here — it drifted to 0.3.3
# while pyproject.toml said 0.4.0, so `make push` tagged the image with the
# wrong version. See docs/adr/0003-single-source-of-truth-for-version.md
VERSION    := $(shell grep -m1 '^version = ' pyproject.toml | cut -d'"' -f2)
ENV_FILE   := $(HOME)/.config/mcp-opensearch/.env

.PHONY: build run shell push

build:
	docker build -t $(IMAGE) .

run:
	docker run --rm -i --network host \
		--env-file $(ENV_FILE) \
		$(IMAGE)

shell:
	docker run --rm -it --network host \
		--env-file $(ENV_FILE) \
		--entrypoint bash \
		$(IMAGE)

push:
	@test -n "$(VERSION)" || { echo "VERSION is empty — could not read it from pyproject.toml"; exit 1; }
	docker build -t $(GHCR_IMAGE):$(VERSION) -t $(GHCR_IMAGE):latest .
	docker push $(GHCR_IMAGE):$(VERSION)
	docker push $(GHCR_IMAGE):latest
