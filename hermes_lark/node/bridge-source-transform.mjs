import { createHash } from "node:crypto";

/** Reviewed upstream source digest for the document-comment override. */
export const DOC_COMMENTS_SOURCE_SHA256 =
  "a0f6b438befdb80f63036bfe507d6ba21d7d2bae17e6454a04b9548817e7b4bb";

/**
 * Source-transform result.
 *
 * @typedef {object} SourceTransformResult
 * @property {string} code Transformed source code.
 * @property {null} map No source map.
 */

/**
 * Verify and transform the reviewed document-comment source.
 *
 * @param {string} code Source module text.
 * @param {string} expectedDigest Reviewed source digest.
 * @returns {SourceTransformResult} Transformed module and source map.
 */
export function transformDocCommentsSource(code, expectedDigest) {
  const normalizedCode = code.replace(/\r\n?/g, "\n");
  const sourceDigest = createHash("sha256")
    .update(normalizedCode)
    .digest("hex");
  if (sourceDigest !== expectedDigest) {
    throw new Error(
      `Expected doc-comment source ${expectedDigest}, found ${sourceDigest}`,
    );
  }

  const tenantCallPattern = /\{ as: 'tenant' \}/g;
  const tenantCalls = normalizedCode.match(tenantCallPattern) ?? [];
  if (tenantCalls.length !== 6) {
    throw new Error(
      `Expected 6 tenant doc-comment calls, found ${tenantCalls.length}`,
    );
  }

  const rawRequestPattern =
    /\(sdk\) => \(sdk as any\)\.request\(\{([\s\S]*?)\n                \}\),\n                \{ as: 'user' \},/g;
  const userIdentityCode = normalizedCode.replaceAll(
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
      (_match, payload) =>
        `(sdk, opts) => (sdk as any).request({${payload}\n                }, opts),\n                { as: 'user' },`,
    ),
    map: null,
  };
}
