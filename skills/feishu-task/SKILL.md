---
name: feishu-task
description: |
  Tools for creating, querying, and updating Feishu tasks and task lists.

  **Use this skill when:**
  (1) Creating, querying, or updating tasks
  (2) Creating or managing task lists
  (3) Listing tasks or the tasks in a task list
  (4) The user mentions tasks, to-dos, task lists, or checklists
  (5) Assigning owners or followers, setting deadlines, or adding members
  (6) Appending task step records through Task `steps`
  (7) Uploading task attachments for `task` or `task_delivery`
  (8) Registering a task Agent or updating its profile through `register` or `update_profile`
---

# Feishu Task Management

## 🚨 Read before use

- ✅ **Time format:** Use ISO 8601 or RFC 3339 with a time zone, for example `2026-02-28T17:00:00+08:00`.
- ✅ **Authentication identity:** `auth_type` may be `user` (the default user identity) or `tenant` (application identity).
- ✅ **Task Agent:** `feishu_task_agent` supports only `tenant` identity, not `user` identity.
- ✅ **`current_user_id` is strongly recommended:** Obtain the sender's `ou_...` ID from message context. If that user is absent from `members`, the tool automatically adds them as a follower so the creator can edit the task.
- ✅ **`patch` and `get` require:** `task_guid`.
- ✅ **`tasklist.tasks` requires:** `tasklist_guid`.
- ✅ **Complete a task:** Set `completed_at` to a date-time string such as `"2026-02-26 15:00:00"`.
- ✅ **Restore a completed task:** Set `completed_at` to the string `"0"`.
- ✅ **`append_steps` timestamps:** Each `task_steps[].timestamp` is a 10-digit Unix timestamp in seconds, not a 13-digit millisecond timestamp.

---

## 📋 Quick index: intent → tool → required parameters

| User intent | Tool | Action | Required parameters | Strongly recommended | Common optional parameters |
| --- | --- | --- | --- | --- | --- |
| Create a to-do | `feishu_task_task` | `create` | `summary` | `current_user_id` from sender ID | `members`, `due`, `description`, `auth_type` |
| List incomplete tasks | `feishu_task_task` | `list` | — | `completed=false` | `page_size`, `auth_type`, `agent_task_status` |
| Get task details | `feishu_task_task` | `get` | `task_guid` | — | `auth_type` |
| Complete a task | `feishu_task_task` | `patch` | `task_guid`, `completed_at` | — | `auth_type` |
| Restore an incomplete task | `feishu_task_task` | `patch` | `task_guid`, `completed_at="0"` | — | `auth_type` |
| Change a deadline | `feishu_task_task` | `patch` | `task_guid`, `due` | — | `auth_type` |
| Add task members | `feishu_task_task` | `add_members` | `task_guid`, `members[]` | — | `auth_type` |
| Append task steps | `feishu_task_task` | `append_steps` | `task_guid`, `idempotent_key`, `task_steps[]` | 10-digit second timestamps | — |
| Create a task list | `feishu_task_tasklist` | `create` | `name` | — | `members` |
| List tasks in a task list | `feishu_task_tasklist` | `tasks` | `tasklist_guid` | — | `completed` |
| Add task-list members | `feishu_task_tasklist` | `add_members` | `tasklist_guid`, `members[]` | — | — |
| Upload a task attachment | `feishu_task_attachment` | `upload` | `resource_id`, base64 `file` | `name` | `resource_type` |
| Register a task Agent | `feishu_task_agent` | `register` | — | `tenant` identity only | — |
| Update a task Agent profile | `feishu_task_agent` | `update_profile` | `profile_content` | `tenant` identity only | — |

---

## 🎯 Important constraints not expressed by the schema

### 1. Authentication identity and visibility (`auth_type`)

The tools support two identities:

- **`user` (default):** Uses a `user_access_token`. Use it when an operation must strictly represent the user or query their private tasks.
  - A user can view and edit only tasks where they are a member.
  - If the creator is not added as a member, they cannot edit the task later.
- **`tenant`:** Uses a `tenant_access_token`. Use application identity when user identity is not appropriate. A user may not see a task unless they are explicitly added as a member.

Automatic protection:

- Pass `current_user_id` from the message sender ID.
- If `members` does not include `current_user_id`, the tool adds that user as a follower.
- This keeps the creator able to view and edit the task.

### 2. Task member roles and types

Roles:

- **`assignee`:** Responsible for completing the task and may edit it.
- **`follower`:** Follows progress and receives notifications.

Types:

- **`user` (default):** An ordinary Feishu user.
- **`app`:** An application or bot. Use this type when adding the current bot or another app.

Example:

```jsonc
{
  "members": [
    {"id": "ou_xxx", "role": "assignee", "type": "user"},
    {"id": "cli_yyy", "role": "follower", "type": "app"}
  ]
}
```

The default `id` format is `open_id`.

### 3. Task-list role conflicts

When creating a task list with `tasklist.create`, the returned `tasklist.members` may omit the creator even when they were included in `members`. The creator automatically becomes the task-list `owner`, and one user may hold only one role, so Feishu removes that user from `members`.

Do not include the creator in `members`; add only other collaborators.

### 4. Three uses of `completed_at`

Complete a task by setting its completion time:

```jsonc
{
  "action": "patch",
  "task_guid": "xxx",
  "completed_at": "2026-02-26 15:30:00"
}
```

Restore a task to incomplete state with the special string `"0"`:

```jsonc
{
  "action": "patch",
  "task_guid": "xxx",
  "completed_at": "0"
}
```

A millisecond timestamp string is also accepted, but should be used only when the caller generates it reliably:

```json
{
  "completed_at": "1740545400000"
}
```

### 5. Task-list member roles

| Member type | Role | Meaning |
| --- | --- | --- |
| `user` | `owner` | Owner who can transfer ownership |
| `user` | `editor` | Can edit the task list and its tasks |
| `user` | `viewer` | Read-only access |
| `chat` | `editor` or `viewer` | Grants access to the entire group chat |

The creator automatically becomes the owner and does not need to appear in `members`.

---

## 📌 Examples

### Scenario 1: Create a task and assign an owner

```json
{
  "action": "create",
  "summary": "Prepare weekly meeting materials",
  "description": "Summarize this week's progress and next week's plan",
  "current_user_id": "ou_sender_open_id",
  "auth_type": "tenant",
  "due": {
    "timestamp": "2026-02-28 17:00:00",
    "is_all_day": false
  },
  "members": [
    {"id": "ou_collaborator_open_id", "role": "assignee", "type": "user"}
  ]
}
```

- `summary` is required.
- Obtain `current_user_id` from the sender ID whenever possible; the tool adds that user as a follower.
- `members` may contain only the other collaborators.
- Prefer an ISO 8601 value with an explicit time zone for time inputs.

### Scenario 2: List my incomplete tasks

```json
{
  "action": "list",
  "completed": false,
  "page_size": 20,
  "auth_type": "user"
}
```

### Scenario 3: Add a bot or user to an existing task

```json
{
  "action": "add_members",
  "task_guid": "task-guid",
  "auth_type": "tenant",
  "members": [
    {"id": "cli_bot_app_id", "role": "follower", "type": "app"}
  ]
}
```

### Scenario 4: Complete a task

```json
{
  "action": "patch",
  "task_guid": "task-guid",
  "completed_at": "2026-02-26 15:30:00"
}
```

### Scenario 5: Restore a task to incomplete state

```json
{
  "action": "patch",
  "task_guid": "task-guid",
  "completed_at": "0"
}
```

### Scenario 6: Create a task list with collaborators

```json
{
  "action": "create",
  "name": "Product Iteration v2.0",
  "members": [
    {"id": "ou_xxx", "role": "editor"},
    {"id": "ou_yyy", "role": "viewer"}
  ]
}
```

### Scenario 7: List incomplete tasks in a task list

```json
{
  "action": "tasks",
  "tasklist_guid": "tasklist-guid",
  "completed": false
}
```

### Scenario 8: Create an all-day task

```json
{
  "action": "create",
  "summary": "Annual Review",
  "due": {
    "timestamp": "2026-03-01 00:00:00",
    "is_all_day": true
  }
}
```

### Scenario 9: Register a task Agent with application identity

```json
{
  "action": "register",
  "auth_type": "tenant"
}
```

### Scenario 10: Update a task Agent profile with application identity

```json
{
  "action": "update_profile",
  "auth_type": "tenant",
  "profile_content": "some profile content"
}
```

---

## 🔍 Common errors and troubleshooting

| Symptom | Cause | Resolution |
| --- | --- | --- |
| Cannot edit a newly created task | Creator was not added to `members` | Add the sender as an `assignee` or `follower` during creation, preferably through `current_user_id`. |
| `patch` reports a missing `task_guid` | `task_guid` was omitted | `patch`, `get`, and `add_members` require `task_guid`. |
| `tasks` reports a missing `tasklist_guid` | `tasklist_guid` was omitted | The `tasklist.tasks` action requires `tasklist_guid`. |
| Restoring an incomplete task fails | Invalid `completed_at` format | Use the string `"0"`, not numeric `0`. |
| Time is incorrect | A Unix timestamp or time without the intended zone was used | Prefer ISO 8601 with a zone, such as `2024-01-01T00:00:00+08:00`. |
| Adding a bot fails | Member `type` was not set to `app` | Set the bot member's `type` to `"app"`. |

---

## 📚 Appendix

### A. Resource hierarchy

```text
Task list
  └─ Section (optional)
      └─ Task
          ├─ Members: assignees and followers
          ├─ Subtasks
          ├─ Due time and start time
          └─ Attachments and comments
```

Core concepts:

- **Task:** An independent to-do with a unique `task_guid`.
- **Task list:** A container for tasks with a unique `tasklist_guid`.
- **Assignee:** May edit and complete a task.
- **Follower:** Receives task update notifications.
- **MyTasks:** The collection of tasks assigned to the current user.

### B. Obtain GUIDs

- `task_guid`: Read `task.guid` from a create response or query tasks with `list`.
- `tasklist_guid`: Read `tasklist.guid` from a create response or query task lists with `list`.

### C. Add a task to a task list

Supply `tasklists` while creating the task:

```json
{
  "action": "create",
  "summary": "Task title",
  "tasklists": [
    {
      "tasklist_guid": "tasklist-guid",
      "section_guid": "optional-section-guid"
    }
  ]
}
```

### D. Create a recurring task

Use an RFC 5545 `RRULE` in `repeat_rule`:

```json
{
  "action": "create",
  "summary": "Weekly meeting",
  "due": {"timestamp": "2026-03-03 14:00:00", "is_all_day": false},
  "repeat_rule": "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO"
}
```

A recurring task must have a due time.

### E. Data permissions

- You can operate only on tasks where you have permission, normally because you are a member.
- You can operate only on task lists where you are a member.
- Adding a task to a task list requires edit permission for both resources.
