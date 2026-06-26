IMAGE      := opensearch-mcp:dev
GHCR_IMAGE := ghcr.io/bunnyiesart/mcp-opensearch
VERSION    := 0.3.0
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
	docker build -t $(GHCR_IMAGE):$(VERSION) -t $(GHCR_IMAGE):latest .
	docker push $(GHCR_IMAGE):$(VERSION)
	docker push $(GHCR_IMAGE):latest
