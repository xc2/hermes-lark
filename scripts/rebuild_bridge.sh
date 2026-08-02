#!/usr/bin/env bash
set -euo pipefail

# Rebuild the vendored bridge only from the reviewed upstream revision.
readonly EXPECTED_COMMIT="dde0be3680d6fd5443cab426c8f4b3216266346a"
readonly REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly UPSTREAM_SOURCE="${1:-${OPENCLAW_LARK_SOURCE:-}}"
readonly BUNDLE_PATH="${REPOSITORY_ROOT}/hermes_lark/node/openclaw_tools_bridge.mjs"

if [[ -z "${UPSTREAM_SOURCE}" ]]; then
  echo "Usage: scripts/rebuild_bridge.sh /path/to/openclaw-lark" >&2
  exit 2
fi

actual_commit="$(git -C "${UPSTREAM_SOURCE}" rev-parse HEAD)"
if [[ "${actual_commit}" != "${EXPECTED_COMMIT}" ]]; then
  echo "Expected openclaw-lark ${EXPECTED_COMMIT}, found ${actual_commit}." >&2
  exit 1
fi

pnpm --dir "${UPSTREAM_SOURCE}" install --frozen-lockfile
OPENCLAW_LARK_SOURCE="${UPSTREAM_SOURCE}" \
  pnpm --dir "${UPSTREAM_SOURCE}" exec tsdown \
  --config "${REPOSITORY_ROOT}/hermes_lark/node/tsdown.bridge.config.ts"

if command -v sha256sum >/dev/null 2>&1; then
  digest="$(sha256sum "${BUNDLE_PATH}" | awk '{print $1}')"
else
  digest="$(shasum -a 256 "${BUNDLE_PATH}" | awk '{print $1}')"
fi
echo "${digest}  openclaw_tools_bridge.mjs" > "${BUNDLE_PATH}.sha256"
echo "Rebuilt ${BUNDLE_PATH} from ${EXPECTED_COMMIT}."
