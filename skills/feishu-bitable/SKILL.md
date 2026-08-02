---
name: feishu-bitable
description: |
  Tools for creating, querying, editing, and managing Feishu Bitable apps. Supports 27 field types, advanced filters, batch operations, and view management.

  **Use this skill when:**
  (1) Creating or managing a Feishu Bitable app
  (2) Creating, querying, updating, or deleting Bitable records (rows)
  (3) Managing fields (columns), views, or tables
  (4) The user mentions Bitable, tables, records, fields, or structured data
  (5) Importing data in bulk or batch-updating Bitable records
---

# Feishu Bitable Skill

## 🚨 Read before use

- ✅ **Creating a table:** Two modes are supported. When requirements are clear, define all fields at once through `table.fields` during `create` to reduce API calls. For exploratory work, start with the default table and modify its fields incrementally; this is easier to adjust and generally more reliable.
- ⚠️ **Empty rows in the default table:** The default table created by `app.create` contains empty records. Before inserting data, call `feishu_bitable_app_table_record.list`, then remove empty rows with `batch_delete` to avoid contaminating the dataset.
- ✅ **Before writing records:** Call `feishu_bitable_app_table_field.list` to obtain each field's `type` and `ui_type`.
- ✅ **User fields:** The default identifier is an `open_id` (`ou_...`). Values must use the array-of-objects form `[{"id":"ou_xxx"}]`.
- ✅ **Date fields:** Use a millisecond timestamp such as `1674206443000`, not seconds.
- ✅ **Single-select fields:** Use a string such as `"Option 1"`, not an array.
- ✅ **Multi-select fields:** Use a string array such as `["Option 1", "Option 2"]`.
- ✅ **Attachment fields:** Upload the file to the current Bitable app first, then use the returned `file_token`.
- ✅ **Batch limit:** A batch may contain at most 500 records. Split larger workloads into multiple batches. Batch operations are atomic.
- ✅ **Concurrency limit:** Concurrent writes to the same table are unsupported. Serialize calls and wait 0.5–1 second between them.

---

## 📋 Quick index: intent → tool → required parameters

| User intent | Tool | Action | Required parameters | Common optional parameters |
| --- | --- | --- | --- | --- |
| List a table's fields | `feishu_bitable_app_table_field` | `list` | `app_token`, `table_id` | — |
| Query records | `feishu_bitable_app_table_record` | `list` | `app_token`, `table_id` | `filter`, `sort`, `field_names` |
| Create one row | `feishu_bitable_app_table_record` | `create` | `app_token`, `table_id`, `fields` | — |
| Import in bulk | `feishu_bitable_app_table_record` | `batch_create` | `app_token`, `table_id`, `records` (≤500) | — |
| Update one row | `feishu_bitable_app_table_record` | `update` | `app_token`, `table_id`, `record_id`, `fields` | — |
| Update in bulk | `feishu_bitable_app_table_record` | `batch_update` | `app_token`, `table_id`, `records` (≤500) | — |
| Create a Bitable app | `feishu_bitable_app` | `create` | `name` | `folder_token` |
| Create a table | `feishu_bitable_app_table` | `create` | `app_token`, `name` | `fields` |
| Create a field | `feishu_bitable_app_table_field` | `create` | `app_token`, `table_id`, `field_name`, `type` | `property` |
| Create a view | `feishu_bitable_app_table_view` | `create` | `app_token`, `table_id`, `view_name`, `view_type` | — |

---

## 🎯 Important constraints not expressed by the schema

### 📚 Detailed references

Consult these documents when configuring fields, formatting record values, or looking for complete examples:

- **[Field property configuration](references/field-properties.md):** The `property` structures required to create or update each field type, including select options, progress ranges, and linked-table IDs.
- **[Record value formats](references/record-values.md):** The value format expected in `fields` for each field type, including user IDs, millisecond timestamps, and uploaded attachments.
- **[Complete examples](references/examples.md):** End-to-end examples covering table-creation modes, batch imports, filtered queries, attachments, and linked fields.

When to consult them:

- A `125408X` error while creating or updating a field usually indicates an invalid `property` structure. See [field-properties.md](references/field-properties.md).
- A `125406X` error while writing a record usually indicates an invalid field value. See [record-values.md](references/record-values.md).
- For complete operation sequences and request bodies, see [examples.md](references/examples.md).

### 1. Field types and value formats must match exactly

The most common Bitable integration problem is that each field type expects a different value structure.

#### Frequently misformatted fields

See [record-values.md](references/record-values.md) for the complete list.

| `type` | `ui_type` | Field type | Correct format | Common mistake |
| --- | --- | --- | --- | --- |
| 11 | `User` | User | `[{id: "ou_xxx"}]` | Passing `"ou_xxx"` or `[{name: "Alice"}]` |
| 5 | `DateTime` | Date and time | `1674206443000` in milliseconds | Passing seconds or a date string |
| 3 | `SingleSelect` | Single select | `"Option name"` | Passing `["Option name"]` |
| 4 | `MultiSelect` | Multi-select | `["Option 1", "Option 2"]` | Passing `"Option 1"` |
| 15 | `Url` | URL | `{link: "...", text: "..."}` | Passing a URL string directly |
| 17 | `Attachment` | Attachment | `[{file_token: "..."}]` | Passing an external URL or local path |

Required workflow:

1. Call `feishu_bitable_app_table_field.list` to retrieve each field's `type` and `ui_type`.
2. Build values using the table above or [record-values.md](references/record-values.md).
3. For error code `125406X` or `1254015`, check the field value format first.

User-field requirements:

- Use `open_id` (`ou_...`) by default, consistently with Calendar and Task tools.
- Use the array-of-objects form `[{id: "ou_xxx"}]`.
- Include only the `id`; do not include `name`, `email`, or other properties.

## 📌 Common workflows

See [examples.md](references/examples.md) for more complete workflows, including table-creation tradeoffs, empty-row cleanup, attachment uploads, and linked fields.

### Scenario 1: Inspect field types first

```json
{
  "action": "list",
  "app_token": "S404b...",
  "table_id": "tbl..."
}
```

The response includes each field's `field_id`, `field_name`, `type`, `ui_type`, and `property`.

### Scenario 2: Import customer records in bulk

```json
{
  "action": "batch_create",
  "app_token": "S404b...",
  "table_id": "tbl...",
  "records": [
    {
      "fields": {
        "Customer Name": "ByteDance",
        "Owner": [{"id": "ou_xxx"}],
        "Contract Date": 1674206443000,
        "Status": "In progress"
      }
    },
    {
      "fields": {
        "Customer Name": "Lark",
        "Owner": [{"id": "ou_yyy"}],
        "Contract Date": 1675416243000,
        "Status": "Complete"
      }
    }
  ]
}
```

Value formats:

- User: `[{id: "ou_xxx"}]` (an array of objects)
- Date and time: millisecond timestamp
- Single select: string
- Multi-select: string array

A batch may contain at most 500 records.

### Scenario 3: Filtered query with advanced filters

```json
{
  "action": "list",
  "app_token": "S404b...",
  "table_id": "tbl...",
  "filter": {
    "conjunction": "and",
    "conditions": [
      {
        "field_name": "Status",
        "operator": "is",
        "value": ["In progress"]
      },
      {
        "field_name": "Due Date",
        "operator": "isLess",
        "value": ["ExactDate", "1740441600000"]
      }
    ]
  },
  "sort": [
    {
      "field_name": "Due Date",
      "desc": false
    }
  ]
}
```

Filter notes:

- Ten operators are supported, including `is`, `isNot`, `contains`, and `isEmpty`; see Appendix B.
- `isEmpty` and `isNotEmpty` require `value: []`. Although these operators need no logical value, the API still requires an empty array.
- Date filters accept values such as `["Today"]` and `["ExactDate", "timestamp"]`.
- `sort` may contain multiple fields.

---

## 🔍 Common errors and troubleshooting

| Error code | Symptom | Cause | Resolution |
| --- | --- | --- | --- |
| `1254064` | `DatetimeFieldConvFail` | Invalid date-time field value | Use a millisecond timestamp such as `1772121600000`; do not use `"2026-02-27"`, RFC 3339, or seconds. |
| `1254068` | `URLFieldConvFail` | Invalid URL field value | Use `{text: "Display text", link: "URL"}` rather than a URL string. |
| `1254066` | `UserFieldConvFail` | Invalid user value or mismatched ID type | Pass `[{id: "ou_xxx"}]` and verify `user_id_type`. |
| `1254015` | `Field types do not match` | Value format does not match the field type | List the fields first, then construct the value for that type. |
| `1254104` | `RecordAddOnceExceedLimit` | More than 500 records in one create request | Split the workload into batches of at most 500. |
| `1254291` | `Write conflict` | Concurrent writes to the same table | Serialize calls and wait 0.5–1 second between them. |
| `1254303` | `AttachPermNotAllow` | Attachment was not uploaded to the current Bitable app | Upload the file before writing the attachment field. |
| `1254045` | `FieldNameNotFound` | Field name does not exist | Check spaces, capitalization, and the exact field name. |

---

## 📚 Appendix: background information

### A. Resource hierarchy

```text
App (Bitable app)
 ├── Table ×100
 │    ├── Record (row) ×20,000
 │    ├── Field (column) ×300
 │    └── View ×200
 └── Dashboard
```

### B. Filter operators

| Operator | Meaning | Supported fields | `value` requirement |
| --- | --- | --- | --- |
| `is` | Equals | All | One value |
| `isNot` | Does not equal | All except date and time | One value |
| `contains` | Contains | All except date and time | One or more values |
| `doesNotContain` | Does not contain | All except date and time | One or more values |
| `isEmpty` | Is empty | All | Must be `[]` |
| `isNotEmpty` | Is not empty | All | Must be `[]` |
| `isGreater` | Greater than | Number, date and time | One value |
| `isGreaterEqual` | Greater than or equal to | Number, not date and time | One value |
| `isLess` | Less than | Number, date and time | One value |
| `isLessEqual` | Less than or equal to | Number, not date and time | One value |

Special date values include `["Today"]`, `["Tomorrow"]`, and `["ExactDate", "timestamp"]`. See [Scenario 3](references/examples.md#scenario-3-filtered-query-with-advanced-filters) for the complete list.

### C. Limits

| Resource | Limit |
| --- | --- |
| Tables plus dashboards | 100 per app |
| Records | 20,000 per table |
| Fields | 300 per table |
| Views | 200 per table |
| Batch create, update, or delete | 500 per API call |
| Text in one cell | 100,000 characters |
| Single-select or multi-select options | 20,000 per field |
| Attachments in one cell | 100 |
| Users in one cell | 1,000 |

### D. Other constraints

- Tables synchronized from another data source do not support creating, deleting, or updating records.
- Formula fields and lookup fields are read-only.
- Deleted data cannot be recovered.
- View filters use `field_id`; call `field.list` first to retrieve it.
