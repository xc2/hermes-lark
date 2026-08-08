"""Behavioral tests for the document-comment source transform."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_CONFIG_PATH = ROOT / "hermes_lark" / "node" / "tsdown.bridge.config.ts"


class BridgeSourceTransformTests(unittest.TestCase):
    """Verify the fail-closed build transform independently of the bundle."""

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_normalizes_crlf_and_rejects_callback_mutation(self) -> None:
        """Equivalent line endings pass while a missing SDK option fails."""
        script = r"""
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { pathToFileURL } from "node:url";

/** Test configuration path and existing source root. */
const [configPath, sourceRoot] = process.argv.slice(1);
process.env.OPENCLAW_LARK_SOURCE = sourceRoot;

/** Production source-transform function under test. */
const { transformDocCommentsSource } = await import(pathToFileURL(configPath));

/** Minimal reviewed-shape source with six selectors and two raw replies. */
const source = [
  "const listCall = client.invoke(",
  "  'list',",
  "  (sdk, opts) => sdk.drive.v1.fileComment.list({}, opts),",
  "  { as: 'tenant' },",
  ");",
  "const replyListCall = { as: 'tenant' };",
  "const createCall = { as: 'tenant' };",
  "const assembledReplyCall = { as: 'tenant' };",
  "const firstReply = (sdk) => (sdk as any).request({",
  "                  data: {},",
  "                }),",
  "                { as: 'tenant' },",
  "const fallbackReply = (sdk) => (sdk as any).request({",
  "                  data: {},",
  "                }),",
  "                { as: 'tenant' },",
].join("\n");

/** Digest of the LF-normalized reviewed source. */
const expectedDigest = createHash("sha256").update(source).digest("hex");

/** Transform results for LF and CRLF checkouts. */
const lfResult = transformDocCommentsSource(source, expectedDigest);
const crlfResult = transformDocCommentsSource(
  source.replaceAll("\n", "\r\n"),
  expectedDigest,
);

assert.deepEqual(crlfResult, lfResult);
assert.doesNotMatch(lfResult.code, /\{ as: 'tenant' \}/);
assert.match(lfResult.code, /\(sdk, opts\) =>/);

/** Mutation that preserves selector counts but drops one SDK option. */
const mutatedSource = source.replace(
  "fileComment.list({}, opts)",
  "fileComment.list({})",
);
assert.throws(
  () => transformDocCommentsSource(mutatedSource, expectedDigest),
  /Expected doc-comment source/,
);
"""
        completed = subprocess.run(
            [
                "node",
                "--input-type=module",
                "--eval",
                script,
                str(BRIDGE_CONFIG_PATH),
                str(ROOT),
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
