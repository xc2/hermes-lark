/**
 * Persistent token-store facade for the Hermes bridge.
 *
 * Tokens use a Hermes-owned AES-256-GCM store so installing this plugin never
 * imports or mutates OpenClaw credentials. Host-supplied one-shot tokens stay
 * only in memory and never become persistent credentials implicitly.
 */

import {
  chmod,
  lstat,
  mkdir,
  readFile,
  rename,
  rm,
  writeFile,
} from "node:fs/promises";
import {
  createCipheriv,
  createDecipheriv,
  createHash,
  randomBytes,
} from "node:crypto";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import {
  maskToken,
  tokenStatus,
  type StoredUAToken,
} from "openclaw-lark-upstream/core/token-store";

/** Token type shared with the pinned upstream implementation. */
export type { StoredUAToken };

/** Master-key length for AES-256. */
const MASTER_KEY_BYTES = 32;

/** Recommended nonce length for AES-GCM. */
const IV_BYTES = 12;

/** Authentication-tag length emitted by Node's AES-GCM implementation. */
const TAG_BYTES = 16;

/** Hermes home used when no explicit credential directory is configured. */
const HERMES_HOME = process.env.HERMES_HOME?.trim()
  ? resolve(process.env.HERMES_HOME)
  : join(homedir(), ".hermes");

/** Private credential directory owned exclusively by this plugin. */
const TOKEN_STORE_DIR = resolve(
  process.env.HERMES_LARK_TOKEN_STORE_DIR?.trim()
    || join(HERMES_HOME, "hermes-lark", "tokens"),
);

/** Master-key path for the encrypted-file backend. */
const MASTER_KEY_PATH = join(TOKEN_STORE_DIR, "master.key");

/** Process-local tokens supplied for exactly one bridge invocation. */
const ephemeralTokens = new Map<string, StoredUAToken>();

/** Build the credential account key. */
function accountKey(appId: string, userOpenId: string): string {
  return `${appId}:${userOpenId}`;
}

/** Build a collision-resistant filename without exposing account identifiers. */
function credentialPath(appId: string, userOpenId: string): string {
  const digest = createHash("sha256")
    .update(accountKey(appId, userOpenId))
    .digest("hex");
  return join(TOKEN_STORE_DIR, `${digest}.enc`);
}

/** Reject empty or unreasonably large credential identifiers. */
function validateIdentity(appId: string, userOpenId: string): void {
  if (!appId || !userOpenId) {
    throw new Error("appId and userOpenId are required");
  }
  if (appId.length > 512 || userOpenId.length > 512) {
    throw new Error("credential identity is too long");
  }
}

/** Ensure the credential directory is private and not a symlink. */
async function ensureTokenStoreDirectory(): Promise<void> {
  await mkdir(TOKEN_STORE_DIR, { recursive: true, mode: 0o700 });
  const info = await lstat(TOKEN_STORE_DIR);
  if (!info.isDirectory() || info.isSymbolicLink()) {
    throw new Error("token store directory must be a real directory");
  }
  await chmod(TOKEN_STORE_DIR, 0o700);
}

/** Load or atomically create the AES-256 master key. */
async function getMasterKey(): Promise<Buffer> {
  await ensureTokenStoreDirectory();
  try {
    const existing = await readFile(MASTER_KEY_PATH);
    if (existing.length !== MASTER_KEY_BYTES) {
      throw new Error("token store master key has an invalid length");
    }
    return existing;
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if (code !== "ENOENT") throw error;
  }

  const candidate = randomBytes(MASTER_KEY_BYTES);
  try {
    await writeFile(MASTER_KEY_PATH, candidate, { flag: "wx", mode: 0o600 });
    await chmod(MASTER_KEY_PATH, 0o600);
    return candidate;
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if (code !== "EEXIST") throw error;
    const existing = await readFile(MASTER_KEY_PATH);
    if (existing.length !== MASTER_KEY_BYTES) {
      throw new Error("token store master key has an invalid length");
    }
    return existing;
  }
}

/** Encrypt one token payload and bind it to its account identity. */
function encryptPayload(
  plaintext: string,
  key: Buffer,
  identity: string,
): Buffer {
  const iv = randomBytes(IV_BYTES);
  const cipher = createCipheriv("aes-256-gcm", key, iv);
  cipher.setAAD(Buffer.from(identity, "utf8"));
  const ciphertext = Buffer.concat([
    cipher.update(plaintext, "utf8"),
    cipher.final(),
  ]);
  return Buffer.concat([iv, cipher.getAuthTag(), ciphertext]);
}

/** Authenticate and decrypt one token payload. */
function decryptPayload(
  data: Buffer,
  key: Buffer,
  identity: string,
): string | null {
  if (data.length < IV_BYTES + TAG_BYTES) return null;
  try {
    const iv = data.subarray(0, IV_BYTES);
    const tag = data.subarray(IV_BYTES, IV_BYTES + TAG_BYTES);
    const ciphertext = data.subarray(IV_BYTES + TAG_BYTES);
    const decipher = createDecipheriv("aes-256-gcm", key, iv);
    decipher.setAAD(Buffer.from(identity, "utf8"));
    decipher.setAuthTag(tag);
    return Buffer.concat([
      decipher.update(ciphertext),
      decipher.final(),
    ]).toString("utf8");
  } catch {
    return null;
  }
}

/** Read a token from the encrypted-file backend. */
async function getPersistentToken(
  appId: string,
  userOpenId: string,
): Promise<StoredUAToken | null> {
  try {
    const key = await getMasterKey();
    const encrypted = await readFile(credentialPath(appId, userOpenId));
    const plaintext = decryptPayload(
      encrypted,
      key,
      accountKey(appId, userOpenId),
    );
    if (!plaintext) return null;
    const token = JSON.parse(plaintext) as StoredUAToken;
    if (token.appId !== appId || token.userOpenId !== userOpenId) return null;
    return token;
  } catch {
    return null;
  }
}

/** Atomically persist a token in the encrypted-file backend. */
async function setPersistentToken(token: StoredUAToken): Promise<void> {
  validateIdentity(token.appId, token.userOpenId);
  const key = await getMasterKey();
  const target = credentialPath(token.appId, token.userOpenId);
  const temporary = join(
    dirname(target),
    `.${createHash("sha256").update(randomBytes(32)).digest("hex")}.tmp`,
  );
  const encrypted = encryptPayload(
    JSON.stringify(token),
    key,
    accountKey(token.appId, token.userOpenId),
  );
  try {
    await writeFile(temporary, encrypted, { flag: "wx", mode: 0o600 });
    await chmod(temporary, 0o600);
    await rename(temporary, target);
    await chmod(target, 0o600);
  } finally {
    await rm(temporary, { force: true });
  }
}

/** Remove a token from the encrypted-file backend. */
async function removePersistentToken(
  appId: string,
  userOpenId: string,
): Promise<void> {
  await rm(credentialPath(appId, userOpenId), { force: true });
}

/** Seed an invocation-local token without writing it to persistent storage. */
export function seedEphemeralToken(token: StoredUAToken): void {
  validateIdentity(token.appId, token.userOpenId);
  ephemeralTokens.set(accountKey(token.appId, token.userOpenId), { ...token });
}

/** Read an ephemeral token first, then the secure persistent store. */
export async function getStoredToken(
  appId: string,
  userOpenId: string,
): Promise<StoredUAToken | null> {
  validateIdentity(appId, userOpenId);
  const ephemeral = ephemeralTokens.get(accountKey(appId, userOpenId));
  if (ephemeral) return { ...ephemeral };
  return getPersistentToken(appId, userOpenId);
}

/** Persist a token through the selected secure credential backend. */
export async function setStoredToken(token: StoredUAToken): Promise<void> {
  validateIdentity(token.appId, token.userOpenId);
  await setPersistentToken(token);
}

/** Remove both invocation-local and persistent credentials for one identity. */
export async function removeStoredToken(
  appId: string,
  userOpenId: string,
): Promise<void> {
  validateIdentity(appId, userOpenId);
  ephemeralTokens.delete(accountKey(appId, userOpenId));
  await removePersistentToken(appId, userOpenId);
}

/** Public safe token masker retained from the upstream store. */
export { maskToken };

/** Public freshness classifier retained from the upstream store. */
export { tokenStatus };
