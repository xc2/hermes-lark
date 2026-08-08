/**
 * Reproducible bundle configuration for the pinned openclaw-lark sources.
 */

import { createHash } from "node:crypto";
import { realpathSync } from "node:fs";
import { resolve } from "node:path";

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

/** Reviewed upstream source digest for the document-comment override. */
const DOC_COMMENTS_SOURCE_SHA256 =
  "a0f6b438befdb80f63036bfe507d6ba21d7d2bae17e6454a04b9548817e7b4bb";

/** Resolve bridge shims and normalize document comments to user identity. */
const bridgeAliasPlugin = {
  name: "hermes-openclaw-tool-bridge-alias",
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
  transform(code: string, id: string): { code: string; map: null } | null {
    if (
      id !==
      resolve(UPSTREAM_ROOT, "src", "tools", "oapi", "drive", "doc-comments.ts")
    ) {
      return null;
    }

    const sourceDigest = createHash("sha256").update(code).digest("hex");
    if (sourceDigest !== DOC_COMMENTS_SOURCE_SHA256) {
      throw new Error(
        `Expected doc-comment source ${DOC_COMMENTS_SOURCE_SHA256}, found ${sourceDigest}`,
      );
    }

    const tenantCallPattern = /\{ as: 'tenant' \}/g;
    const tenantCalls = code.match(tenantCallPattern) ?? [];
    if (tenantCalls.length !== 6) {
      throw new Error(
        `Expected 6 tenant doc-comment calls, found ${tenantCalls.length}`,
      );
    }

    const rawRequestPattern =
      /\(sdk\) => \(sdk as any\)\.request\(\{([\s\S]*?)\n                \}\),\n                \{ as: 'user' \},/g;
    const userIdentityCode = code.replaceAll(
      "{ as: 'tenant' }",
      "{ as: 'user' }",
    );
    const rawRequests = userIdentityCode.match(rawRequestPattern) ?? [];
    if (rawRequests.length !== 2) {
      throw new Error(
        `Expected 2 raw doc-comment reply calls, found ${rawRequests.length}`,
      );
    }

    return {
      code: userIdentityCode.replace(
        rawRequestPattern,
        (_match, payload: string) =>
          `(sdk, opts) => (sdk as any).request({${payload}\n                }, opts),\n                { as: 'user' },`,
      ),
      map: null,
    };
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
  plugins: [bridgeAliasPlugin],
  deps: {
    neverBundle: [/^node:/],
  },
};
