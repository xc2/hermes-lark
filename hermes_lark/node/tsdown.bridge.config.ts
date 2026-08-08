/**
 * Reproducible bundle configuration for the pinned openclaw-lark sources.
 */

import { realpathSync } from "node:fs";
import { resolve } from "node:path";
import {
  DOC_COMMENTS_SOURCE_SHA256,
  transformDocCommentsSource,
} from "./bridge-source-transform.mjs";

/** Pinned source checkout supplied explicitly by the bundle builder. */
const UPSTREAM_SOURCE = process.env.OPENCLAW_LARK_SOURCE?.trim();

if (!UPSTREAM_SOURCE) {
  throw new Error(
    "OPENCLAW_LARK_SOURCE must point to the pinned openclaw-lark checkout",
  );
}

/** Absolute pinned upstream source root used by module aliases. */
const UPSTREAM_ROOT = realpathSync(resolve(UPSTREAM_SOURCE));

/** Directory containing the bridge shims. */
const BRIDGE_ROOT = resolve(import.meta.dirname);

/** Resolve bridge shims and normalize document comments to user identity. */
const bridgeSourcePlugin = {
  name: "hermes-openclaw-tool-bridge-source",
  resolveId(source: string): string | null {
    if (source.startsWith("openclaw-lark-upstream/")) {
      const suffix = source.slice("openclaw-lark-upstream/".length);
      return resolve(UPSTREAM_ROOT, "src", `${suffix}.ts`);
    }
    if (source === "../auto-auth") {
      return resolve(BRIDGE_ROOT, "auto-auth-shim.ts");
    }
    if (source === "./token-store") {
      return resolve(BRIDGE_ROOT, "persistent-token-store.ts");
    }
    if (source === "./uat-client") {
      return resolve(BRIDGE_ROOT, "uat-client-shim.ts");
    }
    return null;
  },
  transform(code: string, id: string) {
    if (
      id !==
      resolve(UPSTREAM_ROOT, "src", "tools", "oapi", "drive", "doc-comments.ts")
    ) {
      return null;
    }

    return transformDocCommentsSource(code, DOC_COMMENTS_SOURCE_SHA256);
  },
};

/** tsdown build definition for a single self-contained ESM artifact. */
export default {
  entry: {
    openclaw_tools_bridge: resolve(BRIDGE_ROOT, "bridge-entry.ts"),
  },
  format: "esm",
  target: "node22",
  platform: "node",
  shims: true,
  clean: false,
  outDir: BRIDGE_ROOT,
  minify: true,
  treeshake: true,
  banner:
    "/*! Bundled from larksuite/openclaw-lark commit dde0be3680d6fd5443cab426c8f4b3216266346a with the Hermes doc-comments user-identity override (MIT). */",
  plugins: [bridgeSourcePlugin],
  deps: {
    neverBundle: [/^node:/],
  },
};
