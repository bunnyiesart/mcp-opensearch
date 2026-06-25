IMAGE    := opensearch-mcp:dev
ENV_FILE := $(HOME)/.config/mcp-opensearch/.env

.PHONY: build run shell

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
