---
name: feishu-calendar
description: |
  Manage Feishu calendars and events, including calendars, events, attendees, and free/busy queries.
---

# Feishu Calendar Management (feishu-calendar)

## Read Before You Start

- **Fixed time zone**: Asia/Shanghai (UTC+8)
- **Time format**: ISO 8601 / RFC 3339 with a time-zone offset, for example `2026-02-25T14:00:00+08:00`
- **Minimum fields for `create`**: `summary`, `start_time`, and `end_time`
- **`user_open_id` is strongly recommended**: Obtain it from `SenderId` (`ou_xxx`) so the user can see and participate in the event
- **ID formats**: User `ou_...`, chat `oc_...`, meeting room `omm_...`, and email `email@...`

---

## Quick Reference: Intent → Tool → Required Parameters

| User intent | Tool | Action | Required parameters | Strongly recommended | Common optional parameters |
| --- | --- | --- | --- | --- | --- |
| Create a meeting | `feishu_calendar_event` | `create` | `summary`, `start_time`, `end_time` | `user_open_id` | `attendees`, `description`, `location` |
| List events in a time range | `feishu_calendar_event` | `list` | `start_time`, `end_time` | - | - |
| Change an event's time | `feishu_calendar_event` | `patch` | `event_id`, `start_time` and/or `end_time` | - | `summary`, `description` |
| Search events by keyword | `feishu_calendar_event` | `search` | `query` | - | - |
| Respond to an invitation | `feishu_calendar_event` | `reply` | `event_id`, `rsvp_status` | - | - |
| List recurring-event instances | `feishu_calendar_event` | `instances` | `event_id`, `start_time`, `end_time` | - | - |
| Query free/busy status | `feishu_calendar_freebusy` | `list` | `time_min`, `time_max`, `user_ids[]` | - | - |
| Invite attendees | `feishu_calendar_event_attendee` | `create` | `calendar_id`, `event_id`, `attendees[]` | - | - |

---

## Important Constraints Not Expressed by the Schema

### 1. Why should `user_open_id` be supplied?

The tool acts with the user's identity, so the event is created on the user's primary calendar and is visible to that user.

Supplying `user_open_id` also adds the initiator as an **attendee**, which ensures that:

- The initiator receives event notifications.
- The initiator can respond with an RSVP status: accept, decline, or tentative.
- The initiator appears in the attendee list.
- Other attendees can see the initiator.

If it is omitted:

- The user can still see the event, but is not an attendee.
- If other attendees are present, the initiator does not appear in the attendee list, which is usually undesirable.

### 2. Attendee permissions (`attendee_ability`)

The tool defaults to `attendee_ability: "can_modify_event"`, allowing attendees to edit the event and manage participants.

| Value | Capability |
| --- | --- |
| `none` | No permissions |
| `can_see_others` | Can view the attendee list |
| `can_invite_others` | Can invite other attendees |
| `can_modify_event` | Can edit the event; recommended |

### 3. Always use `open_id` values in `ou_...` format

- To create an event: `user_open_id = SenderId`
- To invite an attendee: `attendees[].id = "ou_xxx"`

Do not confuse these ID formats:

- `ou_xxx`: A user's `open_id`; this is the value you should use.
- `user_xxx`: An event-internal `attendee_id` returned by list APIs; use it only as an internal record.

### 4. Meeting-room reservations are asynchronous

After adding a meeting room as a resource attendee, reservation proceeds asynchronously:

1. The API succeeds and returns `rsvp_status: "needs_action"`, meaning the reservation is pending.
2. Feishu processes the reservation in the background.
3. The final status becomes `accept` on success or `decline` on failure.

Use `feishu_calendar_event_attendee.list` to check the resulting `rsvp_status`.

### 5. The `instances` action is only valid for recurring events

The `instances` action requires all of the following:

1. `event_id` identifies a recurring event with a non-empty `recurrence` field.
2. Calling it for a non-recurring event returns an error.

To determine whether an event is recurring:

1. Use the `get` action to retrieve the event.
2. Check that its `recurrence` field exists and is non-empty.
3. If it is, use `instances` to list occurrences.

---

## Usage Examples

### Example 1: Create a meeting and invite attendees

```json
{
  "action": "create",
  "summary": "Project retrospective",
  "description": "Review Q1 project progress",
  "start_time": "2026-02-25 14:00:00",
  "end_time": "2026-02-25 15:30:00",
  "user_open_id": "ou_aaa",
  "attendees": [
    {"type": "user", "id": "ou_bbb"},
    {"type": "user", "id": "ou_ccc"},
    {"type": "resource", "id": "omm_xxx"}
  ]
}
```

### Example 2: List a user's events for the next week

```json
{
  "action": "list",
  "start_time": "2026-02-25 00:00:00",
  "end_time": "2026-03-03 23:59:00"
}
```

### Example 3: Check free/busy status for several users

```json
{
  "action": "list",
  "time_min": "2026-02-25 09:00:00",
  "time_max": "2026-02-25 18:00:00",
  "user_ids": ["ou_aaa", "ou_bbb", "ou_ccc"]
}
```

`user_ids` is an array containing 1-10 users. Meeting-room free/busy queries are not currently supported.

### Example 4: Change an event's time

```json
{
  "action": "patch",
  "event_id": "xxx_0",
  "start_time": "2026-02-25 15:00:00",
  "end_time": "2026-02-25 16:00:00"
}
```

### Example 5: Search events by keyword

```json
{
  "action": "search",
  "query": "project retrospective"
}
```

### Example 6: Respond to an event invitation

```json
{
  "action": "reply",
  "event_id": "xxx_0",
  "rsvp_status": "accept"
}
```

---

## Common Errors and Troubleshooting

| Symptom | Root cause | Resolution |
| --- | --- | --- |
| The initiator is missing from the attendee list | `user_open_id` was omitted | Supply `user_open_id = SenderId` |
| Attendees cannot see other attendees | `attendee_ability` is too restrictive | The tool defaults to `can_modify_event` |
| The event time is wrong | A Unix timestamp was used | Use ISO 8601 with an offset, such as `2024-01-01T00:00:00+08:00` |
| A meeting room remains pending | Room reservation is asynchronous | Wait a few seconds, then use `list` to check `rsvp_status` |
| Updating an event returns a permission error | The current user is not the organizer and attendees cannot edit | Ensure the event uses `attendee_ability: "can_modify_event"` |
| The attendee list cannot be viewed | The current user lacks permission | Ensure the user is the organizer or the event grants at least `can_see_others` |

---

## Appendix: Calendar Concepts

### A. Calendar data model

Feishu Calendar uses a three-level model:

```text
Calendar
  └── Event
       └── Attendee
```

Key concepts:

1. **User primary calendar**: An event is created on the initiating user's primary calendar, where the user can see it.
2. **Attendees**: Adding attendees makes the event appear on their calendars.
3. **Permission model**: `attendee_ability` determines whether attendees can edit the event, invite others, or view the attendee list.

### B. Attendee types

- `type: "user"` with `id: "ou_xxx"`: Feishu user identified by `open_id`
- `type: "chat"` with `id: "oc_xxx"`: Feishu chat
- `type: "resource"` with `id: "omm_xxx"`: Meeting room
- `type: "third_party"` with `id: "email@example.com"`: External email address

### C. Event lifecycle

1. **Create**: Create the event on the user's primary calendar using the user's identity.
2. **Invite attendees**: Share the event through the attendee API.
3. **Collect responses**: Attendees respond with `accept`, `decline`, or `tentative`.
4. **Modify**: The organizer or an attendee with sufficient permission can modify the event.
5. **Delete**: Deleting the event changes its status to `cancelled`.

### D. Calendar types

| Type | Description | Deletable | Editable |
| --- | --- | --- | --- |
| `primary` | Primary calendar; one per user or application | No | Yes |
| `shared` | Calendar created and shared by a user | Yes | Yes |
| `resource` | Meeting-room calendar | No | No |
| `google` | Connected Google Calendar | No | No |
| `exchange` | Connected Exchange calendar | No | No |

### E. RSVP statuses

| Status | Meaning for a user | Meaning for a meeting room |
| --- | --- | --- |
| `needs_action` | No response yet | Reservation pending |
| `accept` | Accepted | Reservation succeeded |
| `tentative` | Tentative | - |
| `decline` | Declined | Reservation failed |
| `removed` | Removed | Removed |

### F. Limits from the Feishu OpenAPI documentation

1. Each event supports at most 3,000 attendees.
2. A single attendee-add request supports at most:
   - 1,000 user attendees
   - 100 meeting rooms
3. A primary calendar cannot be deleted.
4. A meeting-room reservation can fail because of:
   - A time conflict
   - Missing reservation permission
   - Meeting-room configuration restrictions
