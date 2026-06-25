IMAGE    := opensearch-mcp:dev
ENV_FILE := $(HOME)/.config/bluearmory/.env

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
