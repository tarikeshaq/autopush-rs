SHELL := /bin/sh
CARGO = cargo
TESTS_DIR := tests
TEST_RESULTS_DIR ?= workspace/test-results
INTEGRATION_TEST_FILE := $(TESTS_DIR)/integration/test_integration_all_rust.py
LOAD_TEST_DIR := $(TESTS_DIR)/load
POETRY := poetry --directory $(TESTS_DIR)
DOCKER_COMPOSE := docker compose
PYPROJECT_TOML := $(TESTS_DIR)/pyproject.toml
FLAKE8_CONFIG := $(TESTS_DIR)/.flake8
STAGE_SERVER_URL := "wss://autopush.stage.mozaws.net"
STAGE_ENDPOINT_URL := "https://updates-autopush.stage.mozaws.net"

.PHONY: ddb

ddb:
	mkdir $@
	curl -sSL http://dynamodb-local.s3-website-us-west-2.amazonaws.com/dynamodb_local_latest.tar.gz | tar xzvC $@

upgrade:
	$(CARGO) install cargo-edit ||
		echo "\n$(CARGO) install cargo-edit failed, continuing.."
	$(CARGO) upgrade
	$(CARGO) update

integration-test-legacy:
	$(POETRY) -V
	$(POETRY) install --without dev,load --no-root
	$(POETRY) run pytest $(INTEGRATION_TEST_FILE) \
		--junit-xml=$(TEST_RESULTS_DIR)/integration_test_legacy_results.xml \
		-v

integration-test:
	$(POETRY) -V
	$(POETRY) install --without dev,load --no-root
	CONNECTION_BINARY=autoconnect \
		CONNECTION_SETTINGS_PREFIX=autoconnect__ \
		$(POETRY) run pytest $(INTEGRATION_TEST_FILE) \
		--junit-xml=$(TEST_RESULTS_DIR)/integration_test_results.xml \
		-v

lint:
	$(POETRY) -V
	$(POETRY) install --no-root
	$(POETRY) run isort --sp $(PYPROJECT_TOML) -c $(TESTS_DIR)
	$(POETRY) run black --quiet --diff --config $(PYPROJECT_TOML) --check $(TESTS_DIR)
	$(POETRY) run flake8 --config $(FLAKE8_CONFIG) $(TESTS_DIR)
	$(POETRY) run mypy $(TESTS_DIR) --config-file=$(PYPROJECT_TOML)

load:
	SERVER_URL=$(STAGE_SERVER_URL) ENDPOINT_URL=$(STAGE_ENDPOINT_URL) \
	  $(DOCKER_COMPOSE) \
      -f $(LOAD_TEST_DIR)/docker-compose.yml \
      -p autopush-rs-load-tests \
      up --scale locust_worker=1

load-clean:
	$(DOCKER_COMPOSE) \
      -f $(LOAD_TEST_DIR)/docker-compose.yml \
      -p autopush-rs-load-tests \
      down
	docker rmi locust
