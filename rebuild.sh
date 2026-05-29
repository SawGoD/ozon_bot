#!/usr/bin/env bash
# Пересборка ozon-bot + чистка повисших образов этого проекта.
# Usage: ./rebuild.sh [--no-cache]
set -euo pipefail

PROJECT="$(basename "$PWD")"
BUILD_ARGS=()
if [[ "${1:-}" == "--no-cache" ]]; then
    BUILD_ARGS+=("--no-cache")
fi

echo ">> docker compose build ${BUILD_ARGS[*]:-}"
docker compose build ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"}
docker compose up -d

echo
echo ">> прунинг dangling-образов проекта ($PROJECT)"
docker image prune -f \
    --filter "label=com.docker.compose.project=$PROJECT" \
    --filter "dangling=true"

echo
echo ">> текущие образы проекта:"
docker images \
    --filter "label=com.docker.compose.project=$PROJECT" \
    --format "table {{.Repository}}\t{{.Tag}}\t{{.CreatedSince}}\t{{.Size}}"

echo
echo ">> статус контейнера:"
docker ps --filter "name=ozon-bot" --format "table {{.Names}}\t{{.Status}}\t{{.Size}}"
