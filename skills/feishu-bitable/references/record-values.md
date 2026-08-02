# Feishu Bitable record value reference

This document describes the value expected in a record's `fields` object for every supported field type.

> **Source:** Feishu Open Platform [Bitable record data structure](https://go.feishu.cn/s/6lY28723w04)

## 📋 Quick index

| Field type | `type` | Value type | Example | Limit |
| --- | --- | --- | --- | --- |
| [Text](#text-type1) | 1 | string when writing; object list when returned | `"Task description"` | 100,000 characters |
| [Number](#number-type2) | 2 | number | `0.5` | — |
| [Single select](#single-select-type3) | 3 | string | `"In progress"` | 20,000 options per field |
| [Multi-select](#multi-select-type4) | 4 | `array<string>` | `["Approval", "Operations"]` | 20,000 options per field; 1,000 per cell |
| [Date and time](#date-and-time-type5) | 5 | number | `1675526400000` | Unix timestamp in milliseconds |
| [Checkbox](#checkbox-type7) | 7 | boolean | `true` | — |
| [User](#user-type11) | 11 | object list | `[{"id": "ou_xxx"}]` | 1,000 per cell; only `id` when writing |
| [Phone number](#phone-number-type13) | 13 | string | `"17899870000"` | 64 characters |
| [URL](#url-type15) | 15 | object | `{"text": "Lark", "link": "..."}` | — |
| [Attachment](#attachment-type17) | 17 | object list | `[{"file_token": "xxx"}]` | 100 per cell |
| [Unidirectional link](#unidirectional-link-type18) | 18 | object | `{"link_record_ids": [...]}` | 500 per cell |
| [Bidirectional link](#bidirectional-link-type21) | 21 | object | `{"link_record_ids": [...]}` | 500 per cell |
| [Location](#location-type22) | 22 | object | `{"location": "116.3,40.0", ...}` | — |
| [Group chat](#group-chat-type23) | 23 | object list | `[{"id": "oc_xxx"}]` | 10 per cell |
| [Formula or lookup](#formula-or-lookup-type20-type19) | 20/19 | object | `{"type": 1, "value": [...]}` | Read-only |

---

## Text (`type=1`)

### Plain text (`ui_type="Text"`)

**Write format:** String.

```json
{
  "fields": {
    "Task Description": "Maintain the customer relationship"
  }
}
```

**Return format:** Array of objects.

```json
{
  "Task Description": [
    {
      "text": "Maintain the customer relationship",
      "type": "text"
    }
  ]
}
```

**Rich text with a user mention and URL:**

```json
{
  "Task Description": [
    {
      "text": "Ask ",
      "type": "text"
    },
    {
      "text": "@Alice",
      "type": "mention",
      "token": "ou_user123",
      "mentionType": "User",
      "mentionNotify": true,
      "name": "Alice"
    },
    {
      "text": " to review ",
      "type": "text"
    },
    {
      "text": "the Lark website",
      "type": "url",
      "link": "https://www.larksuite.com"
    }
  ]
}
```

Rich-text element types:

| `type` | Meaning | Additional properties |
| --- | --- | --- |
| `"text"` | Plain text | `text` |
| `"mention"` | User or document mention | `token`, `mentionType`, `mentionNotify`, `name` |
| `"url"` | URL | `text`, `link` |

Supported `mentionType` values:

- `"User"`: user
- `"Docx"`: document
- `"Sheet"`: spreadsheet
- `"Bitable"`: Bitable app

### Barcode (`ui_type="Barcode"`)

**Write format:** String.

```json
{
  "fields": {
    "Product Barcode": "FS0001"
  }
}
```

**Return format:**

```json
{
  "Product Barcode": [
    {
      "text": "FS0001",
      "type": "text"
    }
  ]
}
```

### Email (`ui_type="Email"`)

**Write format:** String.

```json
{
  "fields": {
    "Contact Email": "alice@example.com"
  }
}
```

**Return format:**

```json
{
  "Contact Email": [
    {
      "text": "alice@example.com",
      "type": "url",
      "link": "mailto:alice@example.com"
    }
  ]
}
```

---

## Number (`type=2`)

**Write and return format:** Number.

```json
{
  "fields": {
    "Hours": 10,
    "Completion Rate": 0.75,
    "Budget": 5000.50
  }
}
```

Notes:

- Progress (`ui_type="Progress"`): a decimal from 0 through 1.
- Currency (`ui_type="Currency"`): an ordinary number.
- Rating (`ui_type="Rating"`): an integer.

---

## Single select (`type=3`)

**Write format:** Option-name string.

```json
{
  "fields": {
    "Task Status": "In progress"
  }
}
```

Passing an option name that does not exist automatically creates the option:

```jsonc
{
  "fields": {
    "Task Status": "Paused" // Created automatically when absent
  }
}
```

**Return format:** The same string used when writing.

```json
{
  "Task Status": "In progress"
}
```

A field may contain at most 20,000 options.

---

## Multi-select (`type=4`)

**Write format:** Array of strings.

```json
{
  "fields": {
    "Tags": ["Approval Integration", "Office Operations", "Identity Management"]
  }
}
```

Passing option names that do not exist automatically creates them:

```jsonc
{
  "fields": {
    "Tags": ["New Tag 1", "New Tag 2"] // Created automatically when absent
  }
}
```

**Return format:** The same array used when writing.

```json
{
  "Tags": ["Approval Integration", "Office Operations"]
}
```

Limits:

- A field may contain at most 20,000 options.
- One cell may select at most 1,000 options.

---

## Date and time (`type=5`)

**Write and return format:** Unix timestamp in milliseconds.

```jsonc
{
  "fields": {
    "Due Date": 1675526400000 // 2023-02-05 00:00:00 UTC
  }
}
```

Use milliseconds, not seconds. Convert from the intended time zone explicitly.

Common mistakes that cause error `1254064`:

```jsonc
// Incorrect: ISO date string
{"Due Date": "2026-02-27"}

// Incorrect: RFC 3339 string
{"Due Date": "2026-02-27T10:00:00+08:00"}

// Incorrect: timestamp in seconds, missing three digits
{"Due Date": 1772121600}

// Correct: timestamp in milliseconds
{"Due Date": 1772121600000}
```

---

## Checkbox (`type=7`)

**Write and return format:** Boolean.

```json
{
  "fields": {
    "Complete": true,
    "Delayed": false
  }
}
```

---

## User (`type=11`)

**Write format:** Array of objects containing only `id`.

```json
{
  "fields": {
    "Owner": [
      {"id": "ou_8240099442cf5da49f04f4bf8f8abcef"}
    ],
    "Collaborators": [
      {"id": "ou_user1"},
      {"id": "ou_user2"}
    ]
  }
}
```

**Return format:** Array of objects with full profile information.

```json
{
  "Owner": [
    {
      "id": "ou_8240099442cf5da49f04f4bf8f8abcef",
      "name": "Amanda Huang",
      "en_name": "Amanda Huang",
      "email": "amanda@example.com",
      "avatar_url": "https://..."
    }
  ]
}
```

Important requirements:

- Include only `id` when writing; do not include `name`, `email`, or other profile properties.
- The identifier must match the `user_id_type` argument: `open_id`, `union_id`, or `user_id`.
- One cell may contain at most 1,000 users.
- Clear the field with `null` or `[]`.

---

## Phone number (`type=13`)

**Write and return format:** String.

```json
{
  "fields": {
    "Contact Number": "17899870000",
    "Landline": "+8601012345678"
  }
}
```

The value must match `(\+)?\d*` and may contain at most 64 characters.

---

## URL (`type=15`)

**Write and return format:** Object.

```json
{
  "fields": {
    "Reference Link": {
      "text": "Feishu Open Platform",
      "link": "https://open.feishu.cn"
    }
  }
}
```

- `text`: displayed text
- `link`: destination URL

Common mistake that causes error `1254068`:

```jsonc
// Incorrect: URL string
{
  "Reference Link": "https://open.feishu.cn"
}

// Correct: object with text and link
{
  "Reference Link": {
    "text": "Feishu Open Platform",
    "link": "https://open.feishu.cn"
  }
}

// text and link may be identical
{
  "Reference Link": {
    "text": "https://open.feishu.cn",
    "link": "https://open.feishu.cn"
  }
}
```

---

## Attachment (`type=17`)

**Write format:** Array of objects containing only `file_token`.

```json
{
  "fields": {
    "Attachments": [
      {"file_token": "file_token_example_1"},
      {"file_token": "file_token_example_2"}
    ]
  }
}
```

**Return format:** Array of objects with full metadata.

```json
{
  "Attachments": [
    {
      "file_token": "file_token_example_1",
      "name": "58cc930b89.png",
      "type": "image/png",
      "size": 108867,
      "url": "https://open.feishu.cn/open-apis/drive/v1/medias/...",
      "tmp_url": "https://open.feishu.cn/open-apis/drive/v1/medias/batch_get_tmp_download_url?..."
    }
  ]
}
```

Important requirements:

- Call the [media upload API](https://go.feishu.cn/s/63soQp6O80s) first and use its returned `file_token`.
- One cell may contain at most 100 attachments.
- Error `1254303` means the attachment is not mounted in the current Bitable app.

---

## Unidirectional link (`type=18`)

**Write format:** Object containing a `link_record_ids` array.

```json
{
  "fields": {
    "Linked Tasks": {
      "link_record_ids": ["recHTLvO7x", "recbS8zb2m"]
    }
  }
}
```

Simplified array form:

```json
{
  "fields": {
    "Linked Tasks": ["recHTLvO7x", "recbS8zb2m"]
  }
}
```

**Return format:**

```json
{
  "Linked Tasks": {
    "link_record_ids": ["recHTLvO7x", "recbS8zb2m"]
  }
}
```

One cell may link at most 500 records.

---

## Bidirectional link (`type=21`)

**Write and return format:** The same as a unidirectional link.

```json
{
  "fields": {
    "Related Projects": {
      "link_record_ids": ["reclzUoBLn", "rec7bYQoX1"]
    }
  }
}
```

Notes:

- Updating a bidirectional link also updates the corresponding field in the linked table.
- One cell may link at most 500 records.

---

## Location (`type=22`)

**Write format:** Longitude and latitude string.

```json
{
  "fields": {
    "Office Address": "116.397755,39.903179"
  }
}
```

**Return format:** Object with detailed location information.

```json
{
  "Office Address": {
    "location": "116.352681,40.01437",
    "pname": "Beijing",
    "cityname": "Beijing",
    "adname": "Haidian District",
    "address": "10 Xueqing Road",
    "name": "ByteDance",
    "full_address": "ByteDance, 10 Xueqing Road, Haidian District, Beijing"
  }
}
```

- `location`: longitude and latitude as `"longitude,latitude"`
- `pname`: province or equivalent first-level region
- `cityname`: city
- `adname`: district
- `address`: street address
- `name`: place name
- `full_address`: complete formatted address

---

## Group chat (`type=23`)

**Write format:** Array of objects containing only `id`.

```json
{
  "fields": {
    "Collaboration Group": [
      {"id": "oc_d2a947abb78bbbbb12d4cad55fbabcef"}
    ]
  }
}
```

**Return format:** Array of objects with full information.

```json
{
  "Collaboration Group": [
    {
      "id": "oc_d2a947abb78bbbbb12d4cad55fbabcef",
      "name": "Test Team",
      "avatar_url": "https://..."
    }
  ]
}
```

One cell may contain at most 10 group chats.

---

## Formula or lookup (`type=20`, `type=19`)

**Format:** Object containing `type`, `ui_type`, and `value`.

```jsonc
{
  "Delayed": {
    "type": 1,         // Underlying data type
    "ui_type": "Text", // Display type
    "value": [         // Calculated result
      {
        "text": "✅ On schedule",
        "type": "text"
      }
    ]
  }
}
```

- `type`: underlying data type, such as 1 for text, 2 for number, or 5 for date and time
- `ui_type`: display type, such as `"Text"`, `"Number"`, or `"Progress"`
- `value`: calculated result, whose format is determined by `type`

Number formula example:

```json
{
  "Total Price": {
    "type": 2,
    "ui_type": "Currency",
    "value": 1250.50
  }
}
```

Date formula example:

```json
{
  "Calculated Date": {
    "type": 5,
    "ui_type": "DateTime",
    "value": 1675526400000
  }
}
```

Formula fields are read-only and cannot be set through a write API. The `value` structure follows the underlying `type`.

---

## System fields

### Created time (`type=1001`)

**Return format:** Unix timestamp in milliseconds.

```json
{
  "Created At": 1675526400000
}
```

This field is read-only.

### Last modified time (`type=1002`)

**Return format:** Unix timestamp in milliseconds.

```json
{
  "Updated At": 1675612800000
}
```

This field is read-only.

### Created by and modified by (`type=1003`, `type=1004`)

**Return format:** Array of objects, the same as a user field.

```json
{
  "Created By": [
    {
      "id": "ou_8240099442cf5da49f04f4bf8f8abcef",
      "name": "Amanda Huang",
      "en_name": "Amanda Huang",
      "email": "amanda@example.com",
      "avatar_url": "https://..."
    }
  ]
}
```

These fields are read-only.

### Autonumber (`type=1005`)

**Return format:** String.

```json
{
  "Work Order Number": "WO-20240226-0001"
}
```

This field is read-only.

---

## 🔍 Common errors and troubleshooting

### Field type mismatch (`1254015`)

```jsonc
// Incorrect: string supplied to a date field
{
  "fields": {
    "Due Date": "2024-02-26"
  }
}

// Correct
{
  "fields": {
    "Due Date": 1708905600000
  }
}
```

### Invalid user field (`1254066`)

Common causes:

1. Unsupported properties were supplied:

```jsonc
// Incorrect
{
  "Owner": [
    {"name": "Alice"} // Only id is accepted
  ]
}

// Correct
{
  "Owner": [
    {"id": "ou_xxx"}
  ]
}
```

2. `user_id_type` does not match the supplied ID, for example specifying `open_id` while passing a `union_id`.
3. An `open_id` belongs to a different app. Open IDs cannot be used across apps; use a `user_id` when appropriate.

### Attachment not mounted (`1254303`)

Cause: An external `file_token` was supplied directly.

Resolution:

1. Upload the file to the current Bitable app through the [media upload API](https://go.feishu.cn/s/63soQp6O80s).
2. Write the returned `file_token` to the record.

### Field name not found (`1254045`)

Cause: The field name does not match exactly, possibly because of spaces, newlines, or special characters.

Resolution:

1. Use the [list fields API](https://go.feishu.cn/s/62nuKkQlk03) to retrieve the exact name.
2. Check leading and trailing spaces and newline characters.

### URL conversion failed (`1254068`)

Cause: The `text` or `link` property is missing.

```jsonc
// Incorrect: text is missing
{
  "Reference Link": {
    "link": "https://example.com"
  }
}

// Correct
{
  "Reference Link": {
    "text": "Example website",
    "link": "https://example.com"
  }
}
```

---

## 📌 Best practices

### 1. Optimize batch writes

```json
{
  "fields": {
    "Task Name": "Visit customer",
    "Owner": [{"id": "ou_xxx"}],
    "Due Date": 1708905600000,
    "Tags": ["Important", "Urgent"],
    "Complete": false
  }
}
```

- Supply all required fields in one request to avoid repeated calls.
- Include only fields that need a value; the record does not need every column.

### 2. Clear field values

Method 1, pass `null`:

```json
{
  "fields": {
    "Owner": null,
    "Tags": null
  }
}
```

Method 2, pass an empty array or empty string as appropriate for the field type:

```json
{
  "fields": {
    "Owner": [],
    "Task Name": ""
  }
}
```

### 3. Convert timestamps

JavaScript:

```javascript
// Local date-time string to Unix timestamp in milliseconds
const timestamp = new Date("2024-02-26 14:00").getTime(); // 1708927200000

// Unix timestamp in milliseconds to a localized date-time string
const date = new Date(1708927200000).toLocaleString("en-US", {
  timeZone: "Asia/Shanghai",
});
```

Python:

```python
import datetime

# Local date-time to Unix timestamp in milliseconds
dt = datetime.datetime(2024, 2, 26, 14, 0, 0)
timestamp = int(dt.timestamp() * 1000)  # 1708927200000

# Unix timestamp in milliseconds to date-time
dt = datetime.datetime.fromtimestamp(1708927200000 / 1000)
```

### 4. Cascading updates for linked fields

Bidirectional link:

```jsonc
// Update the bidirectional field in Table A
{
  "fields": {
    "Linked Projects": {
      "link_record_ids": ["rec123"]
    }
  }
}
// The corresponding bidirectional field in Table B is updated automatically.
```

Unidirectional link:

```jsonc
// Update only the current table; the linked table is unchanged.
{
  "fields": {
    "Reference Tasks": {
      "link_record_ids": ["rec456"]
    }
  }
}
```

---

## 🔗 References

- [Feishu Open Platform Bitable record data structure](https://go.feishu.cn/s/6lY28723w04)
- [Create record API](https://go.feishu.cn/s/61Y-IrQjU02)
- [Update record API](https://go.feishu.cn/s/6lY28723A04)
- [Upload media API](https://go.feishu.cn/s/63soQp6O80s)
