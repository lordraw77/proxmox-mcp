IMAGE   := lordraw/proxmox-mcp
VERSION := $(shell git describe --tags --exact-match 2>/dev/null || \
           git rev-parse --short HEAD 2>/dev/null || \
           echo "dev")

BUILD_DATE := $(shell date -u +"%Y-%m-%dT%H:%M:%SZ")
VCS_REF    := $(shell git rev-parse --short HEAD 2>/dev/null || echo "unknown")

BUILD_ARGS := \
	--build-arg VERSION=$(VERSION) \
	--build-arg BUILD_DATE=$(BUILD_DATE) \
	--build-arg VCS_REF=$(VCS_REF)

PLATFORMS := linux/amd64,linux/arm64

.PHONY: build push publish release tag-latest clean login help

## build       Build the image for the local platform
build:
	docker build $(BUILD_ARGS) \
		-t $(IMAGE):$(VERSION) \
		-t $(IMAGE):latest \
		.

## push        Push VERSION and latest tags to Docker Hub
push:
	docker push $(IMAGE):$(VERSION)
	docker push $(IMAGE):latest

## publish     Build + push (single-platform, local arch)
publish: build push

## release     Build + push multi-arch (linux/amd64 + linux/arm64) via buildx
release:
	docker buildx build $(BUILD_ARGS) \
		--platform $(PLATFORMS) \
		-t $(IMAGE):$(VERSION) \
		-t $(IMAGE):latest \
		--push \
		.

## tag-latest  Re-tag the current VERSION as latest and push
tag-latest:
	docker tag $(IMAGE):$(VERSION) $(IMAGE):latest
	docker push $(IMAGE):latest

## clean       Remove local image tags
clean:
	docker rmi $(IMAGE):$(VERSION) $(IMAGE):latest 2>/dev/null || true

## login       Log in to Docker Hub (required before push/publish/release)
login:
	docker login -u lordraw

## help        Show this help
help:
	@grep -E '^## ' Makefile | sed 's/## /  make /'

.DEFAULT_GOAL := help
