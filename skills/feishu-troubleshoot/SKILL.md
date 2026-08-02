---
name: feishu-troubleshoot
description: |
  Troubleshooting guidance for the Feishu plugin, including common issues and the `/feishu doctor` diagnostic command (legacy alias: `/feishu_doctor`).

  Consult the FAQ at any time. Use diagnostics for complex authorization failures, such as repeated authorization attempts or an automatic flow that cannot resolve the problem. Diagnostics inspect account configuration, API connectivity, application permissions, and user authorization, then produce a detailed report with suggested resolutions.

---

# Troubleshoot the Feishu Plugin

## ❓ Frequently asked questions

### Card buttons do not respond

**Symptom:** Clicking a card button has no effect and eventually displays an error.

**Cause:** The application has not enabled the card interaction callback.

**Resolution:**

1. Sign in to the [Feishu Open Platform](https://open.feishu.cn/app).
2. Select the application, then open **Events & Callbacks**.
3. Set the subscription method to a persistent WebSocket connection and add the `card.action.trigger` card interaction callback.
4. Create an application version, submit it for review, and publish it.

---

## 🔍 Diagnostic command

Use diagnostics only for complex permission-related problems. Ordinary permission failures automatically start the authorization flow and do not need manual diagnostics.

Run diagnostics when:

- Errors continue after repeated authorization attempts.
- The automatic authorization flow cannot resolve the problem.
- You need the complete permission and account state.

Send the following as a user message in a Feishu conversation:

```text
/feishu doctor
```

The command checks:

- **Diagnostic summary**, displayed first:
  - Overall status: healthy, warning, or failure
  - A concise list of detected issues
- **Environment:**
  - Plugin version
- **Account:**
  - Credential completeness, with a masked `appId` and `appSecret`
  - Whether the account is enabled
  - API connectivity
  - Bot name and `openId`
- **Application-identity permissions:**
  - Number of required scopes enabled for the application
  - Missing required scopes
  - A link that opens the scope request page with missing scopes preselected
- **User-identity permissions:**
  - Counts of valid, refresh-required, and expired user grants
  - Automatic refresh availability, including whether `offline_access` is present
  - Per-scope comparison of application and user grants
  - Instructions and links for missing application scopes
  - Reauthorization steps when the user's grant is insufficient
