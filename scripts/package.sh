#!/usr/bin/env bash
#
# Build a distributable source bundle of the platform.
#
# The bundle is produced from the committed tree via `git archive`, so it contains
# exactly what is under version control at the chosen revision -- no virtualenv, no
# node_modules, no build output, no local database and no .env. A BUILD_INFO.json
# records the revision so an extracted copy can always be traced back to a commit.
#
# Usage:
#   scripts/package.sh                 # bundle HEAD
#   scripts/package.sh v1.0.0          # bundle a tag or commit
#   OUTPUT_DIR=/tmp scripts/package.sh # write elsewhere

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

REVISION="${1:-HEAD}"
OUTPUT_DIR="${OUTPUT_DIR:-$REPO_ROOT/release}"
NAME="finops-ai-command-center"

VERSION="$(grep -m1 '^version' backend/pyproject.toml | cut -d'"' -f2)"
COMMIT="$(git rev-parse "$REVISION")"
SHORT_COMMIT="$(git rev-parse --short "$REVISION")"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
BUNDLE="${NAME}-${VERSION}"
ARCHIVE="${OUTPUT_DIR}/${BUNDLE}.zip"

if ! git diff --quiet "$REVISION" -- 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
  echo "warning: the working tree has uncommitted changes; the bundle reflects ${SHORT_COMMIT}, not your working copy" >&2
fi

STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT

echo "Packaging ${BUNDLE} from ${SHORT_COMMIT} ..."
mkdir -p "$STAGING/$BUNDLE"
git archive --format=tar "$REVISION" | tar -x -C "$STAGING/$BUNDLE"

# A previously released bundle must never end up inside a new one.
rm -rf "$STAGING/$BUNDLE/release"

FILE_COUNT="$(find "$STAGING/$BUNDLE" -type f | wc -l | tr -d ' ')"
cat > "$STAGING/$BUNDLE/BUILD_INFO.json" <<JSON
{
  "name": "${NAME}",
  "version": "${VERSION}",
  "commit": "${COMMIT}",
  "commit_short": "${SHORT_COMMIT}",
  "branch": "${BRANCH}",
  "built_at": "${BUILT_AT}",
  "file_count": ${FILE_COUNT},
  "contents": [
    "backend/         FastAPI application, execution engine, agents, tools, RAG, migrations, tests",
    "apps/execute/    Application 1 - login, Execute and Analytics",
    "apps/console/    Application 2 - the rest of the platform",
    "packages/shared/ Design system, API client and stores shared by both applications",
    "infra/           Kubernetes, Helm, Prometheus, Grafana, OpenTelemetry, nginx",
    "docs/            Architecture, deployment, operations, security, agents, API, process, validation",
    "scripts/         Packaging"
  ],
  "excluded": [
    "backend/.venv", "node_modules", "apps/*/dist",
    "*.db", "backend/var", ".env", ".git"
  ],
  "getting_started": "See README.md. At least one LLM provider key is required to execute agents."
}
JSON

mkdir -p "$OUTPUT_DIR"
rm -f "$ARCHIVE" "${ARCHIVE}.sha256"
( cd "$STAGING" && zip -qr9 "$ARCHIVE" "$BUNDLE" -x '*.pyc' -x '*__pycache__*' )

( cd "$OUTPUT_DIR" && sha256sum "$(basename "$ARCHIVE")" > "$(basename "$ARCHIVE").sha256" )

SIZE="$(du -h "$ARCHIVE" | cut -f1)"
echo
echo "  archive : ${ARCHIVE}"
echo "  size    : ${SIZE}"
echo "  files   : ${FILE_COUNT}"
echo "  commit  : ${SHORT_COMMIT} (${BRANCH})"
echo "  sha256  : $(cut -d' ' -f1 < "${ARCHIVE}.sha256")"
