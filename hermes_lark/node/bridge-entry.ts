/**
 * One-shot Hermes bridge for the tool implementations from openclaw-lark.
 *
 * The build bundles the upstream TypeScript and its runtime dependencies into
 * one ESM file.  OpenClaw's daemon APIs are replaced by the small shims in this
 * directory; the Feishu API implementations themselves remain upstream code.
 */

import { readFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { registerOapiTools } from "openclaw-lark-upstream/tools/oapi/index";
import { registerFeishuMcpDocTools } from "openclaw-lark-upstream/tools/mcp/doc/index";
import { registerFeishuOAuthTool } from "openclaw-lark-upstream/tools/oauth";
import { getLarkAccount } from "openclaw-lark-upstream/core/accounts";
import { LarkClient } from "openclaw-lark-upstream/core/lark-client";
import { withTicket } from "openclaw-lark-upstream/core/lark-ticket";
import {
  getStoredToken,
  removeStoredToken,
  seedEphemeralToken,
  setStoredToken,
  type StoredUAToken,
} from "./persistent-token-store";

/** Tool shape captured from the upstream plugin registration calls. */
interface RegisteredTool {
  /** Public tool name. */
  name: string;
  /** Upstream execution callback. */
  execute: (toolCallId: string, params: unknown) => Promise<unknown>;
}

/** Persistent credential identity accepted by token-store protocol actions. */
interface CredentialIdentity {
  /** Feishu application id. */
  appId: string;
  /** Feishu user open_id. */
  userOpenId: string;
}

/** Request accepted on standard input. */
interface BridgeRequest {
  /** Protocol operation. */
  action: "invoke" | "list" | "token_get" | "token_set" | "token_remove";
  /** Tool to invoke. */
  tool?: string;
  /** Tool arguments. */
  arguments?: Record<string, unknown>;
  /** OpenClaw-shaped Feishu configuration. */
  config: Record<string, unknown>;
  /** Message-scoped identity propagated through AsyncLocalStorage. */
  ticket?: {
    /** Source message id. */
    messageId: string;
    /** Source chat id. */
    chatId: string;
    /** Feishu account id. */
    accountId: string;
    /** Request start time in Unix milliseconds. */
    startTime?: number;
    /** Sender open_id used for UAT selection. */
    senderOpenId?: string;
    /** Source chat type. */
    chatType?: "p2p" | "group";
    /** Source thread id. */
    threadId?: string;
  };
  /** Optional ephemeral user token supplied by the Hermes host. */
  userToken?: {
    /** User access token. */
    accessToken: string;
    /** Refresh token, when available. */
    refreshToken?: string;
    /** Space-delimited granted scopes. */
    scope?: string;
    /** Access-token expiry in Unix milliseconds. */
    expiresAt?: number;
    /** Refresh-token expiry in Unix milliseconds. */
    refreshExpiresAt?: number;
  };
  /** Caller-provided tool call id. */
  toolCallId?: string;
  /** Persistent credential identity for token read and removal. */
  credential?: CredentialIdentity;
  /** Complete token record accepted only by the internal persistence action. */
  storedToken?: StoredUAToken;
}

/** JSON response written to standard output. */
interface BridgeResponse {
  /** Whether the bridge itself completed the operation. */
  ok: boolean;
  /** Upstream result on success. */
  result?: unknown;
  /** Stable bridge error on failure. */
  error?: {
    /** Machine-readable error code. */
    code: string;
    /** Human-readable error message. */
    message: string;
  };
}

/** Captured upstream tools for the current process. */
const tools = new Map<string, RegisteredTool>();

/** Logger accepted by upstream code without contaminating stdout JSON. */
const quietLogger = {
  debug: (_message: string, _meta?: Record<string, unknown>): void => {},
  info: (_message: string, _meta?: Record<string, unknown>): void => {},
  warn: (_message: string, _meta?: Record<string, unknown>): void => {},
  error: (_message: string, _meta?: Record<string, unknown>): void => {},
};

/** Silence the SDK's console logger so stdout remains a single JSON document. */
function silenceSdkConsole(): void {
  const discard = (..._args: unknown[]): void => {};
  console.debug = discard;
  console.info = discard;
  console.log = discard;
  console.warn = discard;
  console.error = discard;
}

/** Parse the single request supplied over stdin. */
function readRequest(): BridgeRequest {
  const raw = readFileSync(0, "utf8").trim();
  if (!raw) throw new Error("bridge request is empty");
  return JSON.parse(raw) as BridgeRequest;
}

/** Build the minimal plugin API needed by upstream tool registration. */
function buildPluginApi(
  config: Record<string, unknown>,
): Record<string, unknown> {
  return {
    config,
    logger: quietLogger,
    registerTool(tool: RegisteredTool): void {
      if (
        tool &&
        typeof tool.name === "string" &&
        typeof tool.execute === "function"
      ) {
        tools.set(tool.name, tool);
      }
    },
  };
}

/** Install a runtime facade used by upstream live-config and logging helpers. */
function installRuntime(config: Record<string, unknown>): void {
  LarkClient.setRuntime({
    config: {
      loadConfig: (): Record<string, unknown> => config,
    },
    logging: {
      getChildLogger: (): typeof quietLogger => quietLogger,
    },
  } as never);
}

/** Seed the one-shot in-memory token store when the host supplies a UAT. */
async function seedUserToken(request: BridgeRequest): Promise<void> {
  const token = request.userToken;
  const ticket = request.ticket;
  const account = ticket
    ? getLarkAccount(request.config as never, ticket.accountId)
    : undefined;
  const appId = account?.configured ? account.appId : "";
  if (!token?.accessToken || !ticket?.senderOpenId || !appId) return;

  const now = Date.now();
  seedEphemeralToken({
    appId,
    userOpenId: ticket.senderOpenId,
    accessToken: token.accessToken,
    refreshToken: token.refreshToken ?? "",
    scope: token.scope ?? "",
    expiresAt: token.expiresAt ?? now + 60 * 60 * 1000,
    refreshExpiresAt: token.refreshExpiresAt ?? now + 30 * 24 * 60 * 60 * 1000,
    grantedAt: now,
  });
}

/** Register the 37 upstream tools whose lifecycle fits one process. */
function registerDirectTools(config: Record<string, unknown>): void {
  const api = buildPluginApi(config) as never;
  registerOapiTools(api);
  registerFeishuMcpDocTools(api);
  registerFeishuOAuthTool(api);
}

/** Validate and normalize one persistent credential identity. */
function requireCredential(request: BridgeRequest): CredentialIdentity {
  const appId = request.credential?.appId?.trim() ?? "";
  const userOpenId = request.credential?.userOpenId?.trim() ?? "";
  if (!appId || !userOpenId) {
    throw new Error("credential.appId and credential.userOpenId are required");
  }
  if (appId.length > 512 || userOpenId.length > 512) {
    throw new Error("credential identity is too long");
  }
  return { appId, userOpenId };
}

/** Validate a host-provided token before it reaches persistent storage. */
function requireStoredToken(request: BridgeRequest): StoredUAToken {
  const token = request.storedToken;
  if (
    !token ||
    typeof token.appId !== "string" ||
    typeof token.userOpenId !== "string" ||
    typeof token.accessToken !== "string" ||
    typeof token.refreshToken !== "string" ||
    typeof token.scope !== "string" ||
    !Number.isFinite(token.expiresAt) ||
    !Number.isFinite(token.refreshExpiresAt) ||
    !Number.isFinite(token.grantedAt)
  ) {
    throw new Error("storedToken is incomplete or invalid");
  }
  if (!token.appId.trim() || !token.userOpenId.trim() || !token.accessToken) {
    throw new Error("storedToken identity and accessToken are required");
  }
  return {
    ...token,
    appId: token.appId.trim(),
    userOpenId: token.userOpenId.trim(),
  };
}

/** Execute one internal credential-store protocol action. */
async function handleTokenOperation(request: BridgeRequest): Promise<unknown> {
  if (request.action === "token_set") {
    const token = requireStoredToken(request);
    await setStoredToken(token);
    return { stored: true };
  }

  const identity = requireCredential(request);
  if (request.action === "token_remove") {
    await removeStoredToken(identity.appId, identity.userOpenId);
    return { removed: true };
  }

  const token = await getStoredToken(identity.appId, identity.userOpenId);
  return token ? { found: true, token } : { found: false };
}

/** Invoke one captured upstream tool inside its message ticket. */
async function invokeTool(request: BridgeRequest): Promise<unknown> {
  if (!request.tool) throw new Error("tool is required for invoke");
  const tool = tools.get(request.tool);
  if (!tool) {
    throw new Error(
      `tool "${request.tool}" is not available in the one-shot bridge; ` +
        "interactive OAuth and AskUserQuestion are handled by the Python lifecycle interface",
    );
  }

  const call = (): Promise<unknown> =>
    tool.execute(request.toolCallId ?? randomUUID(), request.arguments ?? {});
  if (!request.ticket) return call();

  return await withTicket(
    {
      ...request.ticket,
      startTime: request.ticket.startTime ?? Date.now(),
    },
    call,
  );
}

/** Run the bridge protocol and return a serializable response. */
async function main(): Promise<BridgeResponse> {
  try {
    silenceSdkConsole();
    const request = readRequest();

    if (
      request.action === "token_get" ||
      request.action === "token_set" ||
      request.action === "token_remove"
    ) {
      return { ok: true, result: await handleTokenOperation(request) };
    }

    installRuntime(request.config);
    registerDirectTools(request.config);

    if (request.action === "list") {
      return { ok: true, result: [...tools.keys()] };
    }

    await seedUserToken(request);
    return { ok: true, result: await invokeTool(request) };
  } catch (error) {
    return {
      ok: false,
      error: {
        code: "bridge_error",
        message: error instanceof Error ? error.message : String(error),
      },
    };
  }
}

/** Emit exactly one JSON document for the Python parent process. */
void main().then((response) => {
  process.stdout.write(JSON.stringify(response));
});
