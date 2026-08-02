/**
 * One-shot replacement for openclaw-lark's interactive authorization UI.
 *
 * It preserves structured authorization failures without claiming that a
 * short-lived subprocess can receive Feishu card callbacks.  The Python host
 * turns the follow-up contract into a resumable pending interaction.
 */

/** Text block returned to the model. */
interface ToolContent {
  /** OpenClaw content discriminator. */
  type: "text";
  /** Serialized result text. */
  text: string;
}

/** Shape compatible with an OpenClaw agent tool result. */
interface ToolResult {
  /** Text blocks returned to the model. */
  content: ToolContent[];
  /** Structured details consumed by the Python bridge. */
  details: Record<string, unknown>;
}

/** Convert an upstream authorization error into an explicit host follow-up. */
export function handleInvokeErrorWithAutoAuth(
  error: unknown,
  _config?: unknown,
): ToolResult {
  const source = error as {
    /** Upstream error class name. */
    name?: string;
    /** Safe error text. */
    message?: string;
    /** Requested authorization scopes. */
    scopes?: string[];
    /** Missing authorization scopes. */
    missingScopes?: string[];
    /** User scopes required by the failed operation. */
    requiredScopes?: string[];
    /** Complete scope set needed after app authorization succeeds. */
    allRequiredScopes?: string[];
    /** Open Platform API operation name. */
    apiName?: string;
    /** Feishu application id. */
    appId?: string;
    /** Whether application scopes were verified before requesting user auth. */
    appScopeVerified?: boolean;
    /** Whether one or every listed application scope is required. */
    scopeNeedType?: "one" | "all";
    /** Token class used by the failed Open Platform operation. */
    tokenType?: "user" | "tenant";
  };
  const name = source?.name ?? "Error";
  const scopes =
    source?.missingScopes ?? source?.requiredScopes ?? source?.scopes ?? [];

  let kind = "tool_error";
  if (name === "AppScopeMissingError" || name === "AppScopeCheckFailedError") {
    kind = "app_permission";
  } else if (
    name === "NeedAuthorizationError" ||
    name === "UserAuthRequiredError" ||
    name === "UserScopeInsufficientError"
  ) {
    kind = "oauth";
  } else if (name === "OwnerAccessDeniedError") {
    kind = "owner_only";
  }

  const details: Record<string, unknown> = {
    error:
      kind === "tool_error" ? "feishu_tool_error" : "host_callback_required",
    message: source?.message ?? String(error),
    source_error: name,
  };
  if (scopes.length > 0) details.scopes = scopes;
  if (source?.missingScopes?.length)
    details.missing_scopes = source.missingScopes;
  if (source?.requiredScopes?.length)
    details.required_scopes = source.requiredScopes;
  if (source?.allRequiredScopes?.length) {
    details.all_required_scopes = source.allRequiredScopes;
    details.deferred_scopes = source.allRequiredScopes;
    details.user_auth_deferred = true;
  }
  if (source?.apiName) details.api_name = source.apiName;
  if (source?.appId) details.app_id = source.appId;
  if (source?.appScopeVerified !== undefined) {
    details.app_scope_verified = source.appScopeVerified;
  }
  if (source?.scopeNeedType) details.scope_need_type = source.scopeNeedType;
  if (source?.tokenType) details.token_type = source.tokenType;
  if (kind !== "tool_error" && kind !== "owner_only") {
    details.follow_up = {
      kind,
      status: "requires_host_callback",
      callback_api: "hermes_lark.openclaw_tools.resume_interaction",
      ...(source?.allRequiredScopes?.length
        ? { deferred_scopes: source.allRequiredScopes }
        : {}),
    };
  }

  return {
    content: [{ type: "text", text: JSON.stringify(details, null, 2) }],
    details,
  };
}
