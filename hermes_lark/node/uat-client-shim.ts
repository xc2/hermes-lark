/**
 * UAT client with a cross-process refresh lock for one-shot bridge workers.
 *
 * Upstream's in-memory lock is sufficient for its long-lived daemon. Hermes
 * starts one Node process per tool call, so rotated refresh tokens need an
 * operating-system-visible lock and a fresh credential read inside that lock.
 */

import { createHash, randomUUID } from "node:crypto";
import { homedir, tmpdir } from "node:os";
import { join, resolve } from "node:path";
import {
  chmod,
  lstat,
  mkdir,
  readFile,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import type { LarkBrand } from "openclaw-lark-upstream/core/types";
import { resolveOAuthEndpoints } from "openclaw-lark-upstream/core/device-flow";
import { larkLogger } from "openclaw-lark-upstream/core/lark-logger";
import { feishuFetch } from "openclaw-lark-upstream/core/feishu-fetch";
import {
  NeedAuthorizationError,
  REFRESH_TOKEN_RETRYABLE,
  TOKEN_RETRY_CODES,
} from "openclaw-lark-upstream/core/auth-errors";
import {
  type StoredUAToken,
  getStoredToken,
  maskToken,
  removeStoredToken,
  setStoredToken,
  tokenStatus,
} from "./persistent-token-store";

/** UAT call identity and application credentials. */
export interface UATCallOptions {
  /** Feishu user open_id. */
  userOpenId: string;
  /** Feishu application id. */
  appId: string;
  /** Feishu application secret. */
  appSecret: string;
  /** Feishu, Lark, or a custom Open Platform base URL. */
  domain: LarkBrand;
}

/** Non-secret authorization status returned by the OAuth tool. */
export interface UATStatus {
  /** Whether a stored credential exists. */
  authorized: boolean;
  /** Feishu user open_id. */
  userOpenId: string;
  /** Space-delimited granted scopes. */
  scope?: string;
  /** Access-token expiry in Unix milliseconds. */
  expiresAt?: number;
  /** Refresh-token expiry in Unix milliseconds. */
  refreshExpiresAt?: number;
  /** Original grant time in Unix milliseconds. */
  grantedAt?: number;
  /** Current freshness classification. */
  tokenStatus?: "valid" | "needs_refresh" | "expired";
}

/** Minimal owner record used to release only the caller's lock directory. */
interface RefreshLockOwner {
  /** Worker process id. */
  pid: number;
  /** Random ownership proof. */
  nonce: string;
}

/** Result returned after acquiring one cross-process refresh lock. */
interface AcquiredRefreshLock {
  /** Release the lock if this worker still owns it. */
  release: () => Promise<void>;
}

/** Refresh operations already running inside this bridge worker. */
const refreshLocks = new Map<string, Promise<StoredUAToken | null>>();

/** Refresh lock diagnostics use the upstream subsystem logger. */
const log = larkLogger("core/uat-client");

/** A live refresh should never approach this stale-lock recovery window. */
const REFRESH_LOCK_STALE_MS = 10 * 60 * 1000;

/** Short contention delay before checking the credential again. */
const REFRESH_LOCK_RETRY_MS = 50;

/** Private root shared by all bridge workers for the current OS user. */
const REFRESH_LOCK_ROOT = (() => {
  const configured = process.env.HERMES_LARK_UAT_LOCK_DIR?.trim();
  if (configured) return resolve(configured);
  const userNamespace =
    typeof process.getuid === "function"
      ? String(process.getuid())
      : createHash("sha256").update(homedir()).digest("hex").slice(0, 16);
  return join(tmpdir(), `hermes-lark-uat-locks-${userNamespace}`);
})();

/** Build a non-identifying path for one app and user credential. */
function refreshLockPath(key: string): string {
  const digest = createHash("sha256").update(key).digest("hex");
  return join(REFRESH_LOCK_ROOT, digest);
}

/** Create a private real directory for cross-process lock state. */
async function ensureRefreshLockRoot(): Promise<void> {
  await mkdir(REFRESH_LOCK_ROOT, { recursive: true, mode: 0o700 });
  const info = await lstat(REFRESH_LOCK_ROOT);
  if (!info.isDirectory() || info.isSymbolicLink()) {
    throw new Error("UAT lock root must be a real directory");
  }
  if (typeof process.getuid === "function" && info.uid !== process.getuid()) {
    throw new Error("UAT lock root belongs to another operating-system user");
  }
  await chmod(REFRESH_LOCK_ROOT, 0o700);
}

/** Return whether the recorded worker still exists on this host. */
function processIsAlive(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === "EPERM";
  }
}

/** Read a lock owner without surfacing malformed or partial state. */
async function readRefreshLockOwner(
  path: string,
): Promise<RefreshLockOwner | null> {
  try {
    const value = JSON.parse(
      await readFile(join(path, "owner.json"), "utf8"),
    ) as Partial<RefreshLockOwner>;
    if (typeof value.pid !== "number" || typeof value.nonce !== "string")
      return null;
    return { pid: value.pid, nonce: value.nonce };
  } catch {
    return null;
  }
}

/** Return whether a lock can be recovered after its owner disappeared. */
async function refreshLockIsStale(path: string): Promise<boolean> {
  try {
    const [info, owner] = await Promise.all([
      stat(path),
      readRefreshLockOwner(path),
    ]);
    if (owner && !processIsAlive(owner.pid)) return true;
    return Date.now() - info.mtimeMs >= REFRESH_LOCK_STALE_MS;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === "ENOENT";
  }
}

/** Wait without keeping references to credential material. */
function waitForRefreshLock(): Promise<void> {
  return new Promise((resolveWait) =>
    setTimeout(resolveWait, REFRESH_LOCK_RETRY_MS),
  );
}

/** Acquire an atomic directory lock shared by every local bridge process. */
async function acquireRefreshLock(key: string): Promise<AcquiredRefreshLock> {
  await ensureRefreshLockRoot();
  const path = refreshLockPath(key);
  const owner: RefreshLockOwner = { pid: process.pid, nonce: randomUUID() };

  for (;;) {
    try {
      await mkdir(path, { mode: 0o700 });
      await writeFile(join(path, "owner.json"), JSON.stringify(owner), {
        flag: "wx",
        mode: 0o600,
      });
      return {
        release: async (): Promise<void> => {
          const current = await readRefreshLockOwner(path);
          if (current?.pid === owner.pid && current.nonce === owner.nonce) {
            await rm(path, { recursive: true, force: true });
          }
        },
      };
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EEXIST") {
        await rm(path, { recursive: true, force: true });
        throw error;
      }
      if (await refreshLockIsStale(path)) {
        await rm(path, { recursive: true, force: true });
        continue;
      }
      await waitForRefreshLock();
    }
  }
}

/** Refresh one credential after the caller has acquired its lock. */
async function doRefreshToken(
  options: UATCallOptions,
  stored: StoredUAToken,
): Promise<StoredUAToken | null> {
  if (Date.now() >= stored.refreshExpiresAt) {
    log.info(`refresh_token expired for ${options.userOpenId}, clearing`);
    await removeStoredToken(options.appId, options.userOpenId);
    return null;
  }

  const endpoint = resolveOAuthEndpoints(options.domain).token;
  const requestBody = new URLSearchParams({
    grant_type: "refresh_token",
    refresh_token: stored.refreshToken,
    client_id: options.appId,
    client_secret: options.appSecret,
  }).toString();
  const callEndpoint = async (): Promise<Record<string, unknown>> => {
    const response = await feishuFetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: requestBody,
    });
    return (await response.json()) as Record<string, unknown>;
  };

  let data = await callEndpoint();
  const firstCode = data.code as number | undefined;
  const firstError = data.error as string | undefined;
  if ((firstCode !== undefined && firstCode !== 0) || firstError) {
    if (REFRESH_TOKEN_RETRYABLE.has(firstCode as number)) {
      data = await callEndpoint();
      const retryCode = data.code as number | undefined;
      const retryError = data.error as string | undefined;
      if ((retryCode !== undefined && retryCode !== 0) || retryError) {
        await removeStoredToken(options.appId, options.userOpenId);
        return null;
      }
    } else {
      await removeStoredToken(options.appId, options.userOpenId);
      return null;
    }
  }
  if (!data.access_token)
    throw new Error("Token refresh returned no access_token");

  const now = Date.now();
  const updated: StoredUAToken = {
    userOpenId: stored.userOpenId,
    appId: options.appId,
    accessToken: data.access_token as string,
    refreshToken: (data.refresh_token as string) ?? stored.refreshToken,
    expiresAt: now + ((data.expires_in as number) ?? 7200) * 1000,
    refreshExpiresAt: data.refresh_token_expires_in
      ? now + (data.refresh_token_expires_in as number) * 1000
      : stored.refreshExpiresAt,
    scope: (data.scope as string) ?? stored.scope,
    grantedAt: stored.grantedAt,
  };
  await setStoredToken(updated);
  log.info(
    `refreshed UAT for ${options.userOpenId} (at:${maskToken(updated.accessToken)})`,
  );
  return updated;
}

/** Refresh once across workers and reuse a credential another worker rotated. */
async function refreshWithLock(
  options: UATCallOptions,
  observed: StoredUAToken,
  force: boolean,
): Promise<StoredUAToken | null> {
  const key = `${options.appId}:${options.userOpenId}`;
  const existing = refreshLocks.get(key);
  if (existing) {
    await existing;
    return getStoredToken(options.appId, options.userOpenId);
  }

  const promise = (async (): Promise<StoredUAToken | null> => {
    const lock = await acquireRefreshLock(key);
    try {
      const current = await getStoredToken(options.appId, options.userOpenId);
      if (!current) return null;
      const rotated =
        current.accessToken !== observed.accessToken ||
        current.refreshToken !== observed.refreshToken;
      const status = tokenStatus(current);
      if (rotated && status !== "expired") return current;
      if (!force && status === "valid") return current;
      if (status === "expired") {
        await removeStoredToken(options.appId, options.userOpenId);
        return null;
      }
      return await doRefreshToken(options, current);
    } finally {
      await lock.release();
    }
  })();

  refreshLocks.set(key, promise);
  try {
    return await promise;
  } finally {
    refreshLocks.delete(key);
  }
}

/** Obtain a valid access token without exposing it to the model. */
export async function getValidAccessToken(
  options: UATCallOptions,
): Promise<string> {
  const stored = await getStoredToken(options.appId, options.userOpenId);
  if (!stored) throw new NeedAuthorizationError(options.userOpenId);
  const status = tokenStatus(stored);
  if (status === "valid") return stored.accessToken;
  const refreshed = await refreshWithLock(options, stored, false);
  if (!refreshed) throw new NeedAuthorizationError(options.userOpenId);
  return refreshed.accessToken;
}

/** Execute a UAT API call and retry once after server-side token rejection. */
export async function callWithUAT<T>(
  options: UATCallOptions,
  apiCall: (accessToken: string) => Promise<T>,
): Promise<T> {
  const accessToken = await getValidAccessToken(options);
  try {
    return await apiCall(accessToken);
  } catch (error) {
    const value = error as {
      /** Direct SDK error code. */
      code?: number;
      /** Nested HTTP response envelope. */
      response?: {
        /** Response body returned by the API. */
        data?: {
          /** API error code in the response body. */
          code?: number;
        };
      };
    };
    const code = value.code ?? value.response?.data?.code;
    if (!TOKEN_RETRY_CODES.has(code as number)) throw error;
    const stored = await getStoredToken(options.appId, options.userOpenId);
    if (!stored) throw new NeedAuthorizationError(options.userOpenId);
    const refreshed = await refreshWithLock(options, stored, true);
    if (!refreshed) throw new NeedAuthorizationError(options.userOpenId);
    return await apiCall(refreshed.accessToken);
  }
}

/** Revoke one user's stored UAT. */
export async function revokeUAT(
  appId: string,
  userOpenId: string,
): Promise<void> {
  await removeStoredToken(appId, userOpenId);
  log.info(`revoked UAT for ${userOpenId}`);
}

/** Backward-compatible error export used by upstream tool code. */
export { NeedAuthorizationError };
