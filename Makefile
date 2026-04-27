# Pool-Stack Docker Compose helper.
#
# Reads DEV from .env and translates it into the DOCKERFILE / BUILD_CONTEXT
# pair that docker compose expects. This keeps the .env API simple
# (DEV=true|false) without forcing users to remember the two paths.

SHELL := /bin/bash

# Load variables defined in .env so $(DEV) is visible to recipes.
ifneq (,$(wildcard ./.env))
include .env
export
endif

# Lower-case the DEV flag and treat anything that isn't an unambiguous
# "false" as dev mode. This is the same convention systemd / shell scripts
# in pool-stack use.
DEV_LOWER := $(shell printf '%s' '$(DEV)' | tr '[:upper:]' '[:lower:]')

ifeq ($(DEV_LOWER),false)
  DOCKERFILE    := dockerfile-release
  BUILD_CONTEXT := .
else
  DOCKERFILE    := pool-stack-docker-stack/dockerfile-dev
  BUILD_CONTEXT := ..
endif

# Snapshot placeholder path is relative to BUILD_CONTEXT (differs for dev vs release).
ifeq ($(strip $(SNAPSHOT_PATH)),)
  ifeq ($(DEV_LOWER),false)
    export SNAPSHOT_PATH := docker/no-snapshot.marker
  else
    export SNAPSHOT_PATH := pool-stack-docker-stack/docker/no-snapshot.marker
  endif
endif

COMPOSE_ENV := DOCKERFILE='$(DOCKERFILE)' BUILD_CONTEXT='$(BUILD_CONTEXT)'

.PHONY: help build up down logs ps clean miner-up miner-down config

help:
	@echo "Pool-Stack Docker Compose"
	@echo "  DEV               = $(DEV) (effective: $(DEV_LOWER))"
	@echo "  DOCKERFILE        = $(DOCKERFILE)"
	@echo "  BUILD_CONTEXT     = $(BUILD_CONTEXT)"
	@echo ""
	@echo "Targets:"
	@echo "  make build         Build all images using the selected dockerfile"
	@echo "  make up            Start the stack (without the optional miner)"
	@echo "  make miner-up      Start the stack including the cpu miner"
	@echo "  make down          Stop the stack"
	@echo "  make logs          Tail logs"
	@echo "  make ps            List containers"
	@echo "  make config        Render the resolved compose config"
	@echo "  make clean         Stop containers and prune named volumes"

build:
	$(COMPOSE_ENV) docker compose build

up:
	$(COMPOSE_ENV) docker compose up -d

miner-up:
	$(COMPOSE_ENV) docker compose --profile miner up -d

down:
	$(COMPOSE_ENV) docker compose down

logs:
	$(COMPOSE_ENV) docker compose logs -f --tail=200

ps:
	$(COMPOSE_ENV) docker compose ps

config:
	$(COMPOSE_ENV) docker compose config

clean:
	$(COMPOSE_ENV) docker compose down -v
