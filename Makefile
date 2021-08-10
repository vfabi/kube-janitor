.PHONY: test docker push

IMAGE            ?= hjacobs/kube-janitor
VERSION          ?= $(shell git describe --tags --always --dirty)
TAG              ?= $(VERSION)

default: docker

.PHONY: install
install:
	poetry install

.PHONY: lint
lint:
	poetry run pre-commit run --all-files

test: install lint
	poetry run coverage run --source=kube_janitor -m py.test -v
	poetry run coverage report

version:
	sed -i "s/version: v.*/version: v$(VERSION)/" deploy/*/*.yaml
	sed -i "s/kube-janitor:.*/kube-janitor:$(VERSION)/" deploy/*/*.yaml

docker:
	docker build --build-arg "VERSION=$(VERSION)" -t "$(IMAGE):$(TAG)" .
	@echo 'Docker image $(IMAGE):$(TAG) can now be used.'

push: docker
	docker push "$(IMAGE):$(TAG)"
	docker tag "$(IMAGE):$(TAG)" "$(IMAGE):latest"
	docker push "$(IMAGE):latest"

.PHONY: helm-docs
helm-docs:
	@helm-docs -o Values.md

