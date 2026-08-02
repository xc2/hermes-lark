---
name: feishu-im-read
description: |
  Read Feishu IM messages, including chat history, thread replies, cross-chat search, and image or file downloads.

  **Use this skill when**:
  (1) You need historical messages from a group chat or direct message.
  (2) You need replies from a thread.
  (3) You need to search messages across chats by keyword, sender, time, or another filter.
  (4) A message contains an image, file, audio clip, or video that must be downloaded.
  (5) The user asks about chat history, what was said in a chat, thread replies, message search, images, or file downloads.
  (6) You need to filter messages by time range or retrieve additional pages.
---

# Reading Feishu IM Messages

## Read Before You Start

- Every message-reading tool in this skill acts with the user's identity and can read only chats that the user is permitted to access.
- With `feishu_im_user_get_messages`, supply exactly one of `open_id` and `chat_id`.
- When a message contains `thread_id`, decide from the user's intent whether to retrieve its replies with `feishu_im_user_get_thread_messages`.
- When content retrieved with the user's identity contains a resource marker, download it with `feishu_im_user_fetch_resource`. This requires `message_id`, `file_key`, and `type`.

---

## Quick Reference: Intent → Tool

| User intent | Tool | Required parameters | Common optional parameters |
| --- | --- | --- | --- |
| Read group or DM history | `feishu_im_user_get_messages` | Exactly one of `chat_id` or `open_id` | `relative_time`, `start_time`/`end_time`, `page_size`, `sort_rule` |
| Read replies in a thread | `feishu_im_user_get_thread_messages` | `thread_id` (`omt_xxx`) | `page_size`, `sort_rule` |
| Search messages across chats | `feishu_im_user_search_messages` | At least one filter | `query`, `sender_ids`, `chat_id`, `relative_time`, `start_time`/`end_time`, `page_size` |
| Download an image from a message | `feishu_im_user_fetch_resource` | `message_id`, `file_key` (`img_xxx`), `type="image"` | - |
| Download a file, audio clip, or video | `feishu_im_user_fetch_resource` | `message_id`, `file_key` (`file_xxx`), `type="file"` | - |

---

## Core Constraints

### 1. Time ranges: retrieve the complete relevant period

If the user does not specify a time range, infer an appropriate `relative_time` from the request so the result covers the period the user cares about. When the user specifies a time range, use it directly.

### 2. Pagination: retrieve more pages when necessary

- `page_size` accepts 1-50 and defaults to 50.
- When a response has `has_more=true`, pass its `page_token` to retrieve the next page.
- Continue paging when the user needs complete results. For a high-level overview, the first page is usually sufficient.

### 3. Thread replies: expand threads proactively when context matters

When chat history contains a `thread_id`, normally retrieve the latest 10 replies with `page_size: 10` and `sort_rule: "create_time_desc"` to provide complete context.

| Scenario | Behavior |
| --- | --- |
| Read history and understand its context; default | For each relevant `thread_id`, use `feishu_im_user_get_thread_messages` to retrieve the latest 10 replies |
| The user requests the complete conversation, detailed discussion, or all replies | Retrieve all thread replies with `page_size: 50` and `sort_rule: "create_time_asc"`; paginate when necessary |
| The user wants only an overview or explicitly excludes replies | Do not expand threads |

Thread messages cannot be filtered by time because the Feishu API does not support it. Use pagination instead.

### 4. Cross-chat message search

`feishu_im_user_search_messages` can search across all accessible chats.

| Parameter | Meaning |
| --- | --- |
| `query` | Keyword matched against message content |
| `sender_ids` | List of sender `open_id` values |
| `chat_id` | Limit the search to one chat |
| `mention_ids` | List of mentioned users' `open_id` values |
| `message_type` | Message type: `file`, `image`, or `media` |
| `sender_type` | Sender type: `user`, `bot`, or `all`; defaults to `user` |
| `chat_type` | Chat type: `group` or `p2p` |

Every search result also includes `chat_id`, `chat_type` (`p2p` or `group`), and `chat_name`. A DM result additionally includes `chat_partner`, containing the other participant's `open_id` and name.

### 5. Extracting image, file, and media resources

Message content can contain the following resource markers. Download them with `feishu_im_user_fetch_resource`.

| Resource type | Marker in content | `fetch_resource` parameters |
| --- | --- | --- |
| Image | `![image](img_xxx)` | `message_id`=`om_xxx`, `file_key`=`img_xxx`, `type`=`"image"` |
| File | `<file key="file_xxx" .../>` | `message_id`=`om_xxx`, `file_key`=`file_xxx`, `type`=`"file"` |
| Audio | `<audio key="file_xxx" .../>` | `message_id`=`om_xxx`, `file_key`=`file_xxx`, `type`=`"file"` |
| Video | `<video key="file_xxx" .../>` | `message_id`=`om_xxx`, `file_key`=`file_xxx`, `type`=`"file"` |

Combine the message's `message_id` with the `file_key` found in its content.

Files are limited to 100 MB. Stickers and resources embedded in cards cannot be downloaded with this tool.

### 6. Time filtering

`feishu_im_user_get_messages` and `feishu_im_user_search_messages` support time filters. Thread messages do not.

| Method | Parameters | Example |
| --- | --- | --- |
| Relative time | `relative_time` | `today`, `yesterday`, `this_week`, `last_3_days`, `last_24_hours` |
| Exact time | `start_time` and `end_time` | ISO 8601, such as `2026-02-27T00:00:00+08:00` |

- `relative_time` and `start_time`/`end_time` are mutually exclusive.
- Supported `relative_time` values are `today`, `yesterday`, `day_before_yesterday`, `this_week`, `last_week`, `this_month`, `last_month`, and `last_{N}_{unit}`, where `unit` is `minutes`, `hours`, or `days`.

### 7. Choosing between `open_id` and `chat_id`

| Parameter | Format | Use case |
| --- | --- | --- |
| `chat_id` | `oc_xxx` | A known group or DM chat ID |
| `open_id` | `ou_xxx` | A known user ID; resolve and read the DM with that user |

Supply exactly one. Prefer `chat_id` when it is available.

---

## Usage Examples

### Example 1: Read group history and expand a thread

Step 1: Retrieve group messages.

```json
{ "chat_id": "oc_xxx" }
```

Step 2: When a returned message contains `thread_id`, retrieve its latest replies.

```json
{ "thread_id": "omt_xxx", "page_size": 10, "sort_rule": "create_time_desc" }
```

### Example 2: Search messages across chats

```json
{ "query": "project status", "chat_id": "oc_xxx" }
```

### Example 3: Retrieve another page

If the first response contains `has_more: true` and `page_token: "xxx"`, continue with:

```json
{ "chat_id": "oc_xxx", "page_token": "xxx" }
```

### Example 4: Download a resource from a message

```json
{ "message_id": "om_xxx", "file_key": "img_v3_xxx", "type": "image" }
```

---

## Common Errors and Troubleshooting

| Symptom | Root cause | Resolution |
| --- | --- | --- |
| Too few messages are returned | The time range is too narrow or no time filter was supplied | Infer a suitable `relative_time` from the user's intent |
| Message history is incomplete | `has_more` was not checked | When `has_more=true`, continue with `page_token` |
| A thread discussion is incomplete | Its `thread_id` was not expanded | Retrieve thread replies when `thread_id` is present |
| `open_id and chat_id cannot both be provided` | Both parameters were supplied | Supply exactly one |
| `relative_time and start_time/end_time cannot be used together` | Time-filter parameters conflict | Choose one time-filter method |
| `No DM chat found for open_id=xxx` | No DM history exists | Use `chat_id`, or confirm that a DM exists |
| A thread returns no messages | `thread_id` has the wrong format | Confirm that it uses the `omt_xxx` format |
| An image or file download fails | `file_key` does not belong to `message_id` | Confirm that the key came from that message |
| Permission denied | The user has not authorized the app or cannot access the chat | Complete OAuth authorization and confirm chat membership |
