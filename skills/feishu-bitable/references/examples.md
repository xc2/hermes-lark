# Complete Feishu Bitable examples

This document provides end-to-end Bitable workflows, request parameters, and operational cautions.

> **Prerequisites:** Read [Field property configuration](field-properties.md) and [Record value formats](record-values.md) first.

---

## 📋 Contents

1. [Scenario 0: Create a table using either of two approaches](#scenario-0-create-a-table-using-either-of-two-approaches)
2. [Scenario 1: Inspect field types first](#scenario-1-inspect-field-types-first)
3. [Scenario 2: Import customer records in bulk](#scenario-2-import-customer-records-in-bulk)
4. [Scenario 2.5: Create a table and insert records safely](#scenario-25-create-a-table-and-insert-records-safely)
5. [Scenario 3: Filtered query with advanced filters](#scenario-3-filtered-query-with-advanced-filters)
6. [Scenario 4: Update one record](#scenario-4-update-one-record)
7. [Scenario 5: Create a single-select field with options](#scenario-5-create-a-single-select-field-with-options)
8. [Scenario 6: Create progress, currency, and rating fields](#scenario-6-create-progress-currency-and-rating-fields)
9. [Scenario 7: Work with attachments](#scenario-7-work-with-attachments)
10. [Scenario 8: Create a bidirectional link](#scenario-8-create-a-bidirectional-link)

---

## Scenario 0: Create a table using either of two approaches

### Approach A: Define every field at once

Use this approach when all field types and settings are known. It creates the schema atomically in one API call.

**Tool:** `feishu_bitable_app_table`

```json
{
  "action": "create",
  "app_token": "S404b...",
  "table": {
    "name": "Customer Management",
    "default_view_name": "All Customers",
    "fields": [
      {
        "field_name": "Customer Name",
        "type": 1
      },
      {
        "field_name": "Owner",
        "type": 11,
        "property": {
          "multiple": false
        }
      },
      {
        "field_name": "Contract Date",
        "type": 5,
        "property": {
          "date_formatter": "yyyy-MM-dd"
        }
      },
      {
        "field_name": "Status",
        "type": 3,
        "property": {
          "options": [
            {"name": "In progress", "color": 0},
            {"name": "Complete", "color": 10}
          ]
        }
      },
      {
        "field_name": "Amount",
        "type": 2,
        "ui_type": "Currency",
        "property": {
          "currency_code": "CNY",
          "formatter": "0.00"
        }
      }
    ]
  }
}
```

Example response:

```json
{
  "table_id": "tblXXXXXXXX",
  "name": "Customer Management",
  "default_view_id": "vewXXXXXXXX"
}
```

### Approach B: Start with the default table and modify it incrementally

Use this approach when exploring a schema, adjusting it as you work, or validating complex field settings in stages.

Advantages:

- `app.create` provides a default table and fields that can be modified.
- Complex settings such as select options and URL formats can be validated separately.
- A problematic design is easy to revise, for example by replacing a URL field with text.

#### Step 1: Create the app with `feishu_bitable_app`

```json
{
  "action": "create",
  "name": "Customer Management System",
  "folder_token": "fldXXXXXXXX"
}
```

The response contains an `app_token` and the default table's `default_table_id`.

#### Step 2: Inspect the default fields with `feishu_bitable_app_table_field`

```json
{
  "action": "list",
  "app_token": "S404b...",
  "table_id": "tblXXXXXXXX"
}
```

Example response:

```json
{
  "fields": [
    {
      "field_id": "fld001",
      "field_name": "Text",
      "type": 1,
      "ui_type": "Text"
    },
    {
      "field_id": "fld002",
      "field_name": "Number",
      "type": 2,
      "ui_type": "Number"
    }
  ]
}
```

#### Step 3: Rename the default field with `feishu_bitable_app_table_field`

```json
{
  "action": "update",
  "app_token": "S404b...",
  "table_id": "tblXXXXXXXX",
  "field_id": "fld001",
  "field_name": "Customer Name"
}
```

#### Step 4: Create missing fields with `feishu_bitable_app_table_field`

```json
{
  "action": "create",
  "app_token": "S404b...",
  "table_id": "tblXXXXXXXX",
  "field_name": "Owner",
  "type": 11,
  "property": {
    "multiple": false
  }
}
```

#### Step 5: List the empty records with `feishu_bitable_app_table_record`

```json
{
  "action": "list",
  "app_token": "S404b...",
  "table_id": "tblXXXXXXXX"
}
```

The response may contain empty records such as `[{"record_id": "recxxx", "fields": {}}, ...]`.

#### Step 6: Delete empty rows with `feishu_bitable_app_table_record`

```json
{
  "action": "batch_delete",
  "app_token": "S404b...",
  "table_id": "tblXXXXXXXX",
  "records": ["recxxx", "recyyy"]
}
```

#### Step 7: Insert records in bulk with `feishu_bitable_app_table_record`

```json
{
  "action": "batch_create",
  "app_token": "S404b...",
  "table_id": "tblXXXXXXXX",
  "records": [
    {
      "fields": {
        "Customer Name": "ByteDance",
        "Owner": [{"id": "ou_xxx"}],
        "Status": "In progress"
      }
    }
  ]
}
```

Important requirements for Approach B:

- The default table usually contains empty records. Delete them before inserting data.
- Steps 5 and 6 are required; do not skip them.
- This approach is best when field configuration is not yet certain.

---

## Scenario 1: Inspect field types first

Field types require different value formats, so inspect the schema before writing records.

**Tool:** `feishu_bitable_app_table_field`

```json
{
  "action": "list",
  "app_token": "S404b...",
  "table_id": "tblXXXXXXXX"
}
```

Example response:

```json
{
  "fields": [
    {
      "field_id": "fld001",
      "field_name": "Task Name",
      "type": 1,
      "ui_type": "Text",
      "property": {}
    },
    {
      "field_id": "fld002",
      "field_name": "Owner",
      "type": 11,
      "ui_type": "User",
      "property": {
        "multiple": true
      }
    },
    {
      "field_id": "fld003",
      "field_name": "Due Date",
      "type": 5,
      "ui_type": "DateTime",
      "property": {
        "date_formatter": "yyyy-MM-dd HH:mm"
      }
    },
    {
      "field_id": "fld004",
      "field_name": "Status",
      "type": 3,
      "ui_type": "SingleSelect",
      "property": {
        "options": [
          {"id": "optXXX", "name": "In progress", "color": 0},
          {"id": "optYYY", "name": "Complete", "color": 10}
        ]
      }
    }
  ]
}
```

Key response properties:

- `type`: base type, such as 1 for text, 2 for number, or 3 for single select
- `ui_type`: display type, which distinguishes progress, currency, rating, and similar variants
- `property`: settings such as select options or a date formatter

---

## Scenario 2: Import customer records in bulk

**Tool:** `feishu_bitable_app_table_record`

```json
{
  "action": "batch_create",
  "app_token": "S404b...",
  "table_id": "tblXXXXXXXX",
  "records": [
    {
      "fields": {
        "Customer Name": "Example Company",
        "Owner": [{"id": "ou_xxx"}],
        "Contract Date": 1674206443000,
        "Status": "In progress",
        "Amount": 1000000,
        "Tags": ["Important Customer", "Strategic Partnership"],
        "Contact Number": "17899870000",
        "Website": {
          "text": "Example Company website",
          "link": "https://www.example.com"
        }
      }
    },
    {
      "fields": {
        "Customer Name": "Lark",
        "Owner": [{"id": "ou_xxx"}],
        "Contract Date": 1675416243000,
        "Status": "Complete",
        "Amount": 500000,
        "Tags": ["Core Product"],
        "Contact Number": "13800138000"
      }
    }
  ]
}
```

Value formats used above:

- Text: string, such as `"Customer Name"`
- User: array of objects containing only `id`, such as `[{"id": "ou_xxx"}]`
- Date and time: millisecond timestamp, such as `1674206443000`
- Single select: string, such as `"In progress"`
- Multi-select: string array, such as `["Important Customer", "Strategic Partnership"]`
- Number: number, such as `1000000`
- Phone number: string, such as `"17899870000"`
- URL: object, such as `{"text": "Display text", "link": "URL"}`

Example response:

```json
{
  "records": [
    {
      "record_id": "rec001",
      "fields": {}
    },
    {
      "record_id": "rec002",
      "fields": {}
    }
  ]
}
```

A batch may contain at most 500 records. Split larger imports across requests.

---

## Scenario 2.5: Create a table and insert records safely

The default table created by `app.create` contains empty records. Inserting data without removing them contaminates the dataset.

Use Approach B from Scenario 0:

1. Create the app and obtain `app_token` and `default_table_id`.
2. List records in the default table with the `list` action.
3. Remove empty rows with the `batch_delete` action.
4. Insert data with the `batch_create` action.

Incorrect result when steps 2 and 3 are skipped:

```text
| Customer Name | Owner | Status      |
| ------------- | ----- | ----------- |
|               |       |             | ← Existing empty row
| ByteDance     | Alice | In progress | ← New record
| Lark          | Bob   | Complete    | ← New record
```

Correct result after deleting empty rows:

```text
| Customer Name | Owner | Status      |
| ------------- | ----- | ----------- |
| ByteDance     | Alice | In progress |
| Lark          | Bob   | Complete    |
```

---

## Scenario 3: Filtered query with advanced filters

**Tool:** `feishu_bitable_app_table_record`

```json
{
  "action": "list",
  "app_token": "S404b...",
  "table_id": "tblXXXXXXXX",
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
      },
      {
        "field_name": "Priority",
        "operator": "isGreater",
        "value": ["3"]
      }
    ]
  },
  "sort": [
    {
      "field_name": "Due Date",
      "desc": false
    },
    {
      "field_name": "Priority",
      "desc": true
    }
  ],
  "field_names": ["Task Name", "Owner", "Due Date", "Status"],
  "page_size": 100
}
```

### `filter` structure

- `conjunction`: combine conditions with `"and"` or `"or"`
- `conditions`: array of conditions

### Ten supported operators

| Operator | Meaning | Supported fields | `value` format |
| --- | --- | --- | --- |
| `is` | Equals | All | `["value"]` |
| `isNot` | Does not equal | All except date and time | `["value"]` |
| `contains` | Contains | All except date and time | `["value1", "value2"]` |
| `doesNotContain` | Does not contain | All except date and time | `["value1"]` |
| `isEmpty` | Is empty | All | `[]` |
| `isNotEmpty` | Is not empty | All | `[]` |
| `isGreater` | Greater than | Number, date and time | `["value"]` |
| `isGreaterEqual` | Greater than or equal to | Number | `["value"]` |
| `isLess` | Less than | Number, date and time | `["value"]` |
| `isLessEqual` | Less than or equal to | Number | `["value"]` |

### Special date values

```jsonc
// Exact date
{"operator": "is", "value": ["ExactDate", "1702449755000"]}

// Relative dates
{"operator": "is", "value": ["Today"]}
{"operator": "is", "value": ["Tomorrow"]}
{"operator": "is", "value": ["Yesterday"]}
{"operator": "is", "value": ["CurrentWeek"]}
{"operator": "is", "value": ["LastWeek"]}
{"operator": "is", "value": ["TheLastWeek"]}
{"operator": "is", "value": ["TheNextWeek"]}
```

### `sort` structure

- `field_name`: field to sort
- `desc`: `true` for descending order; `false` for ascending order
- Multiple sort fields are applied in array order.

---

## Scenario 4: Update one record

**Tool:** `feishu_bitable_app_table_record`

```json
{
  "action": "update",
  "app_token": "S404b...",
  "table_id": "tblXXXXXXXX",
  "record_id": "recusyQbB0fVL5",
  "fields": {
    "Status": "Complete",
    "Completion Time": 1674206443000,
    "Notes": "The customer signed the contract"
  }
}
```

- Include only fields that need to change.
- Omitted fields keep their current values.
- Partial updates are supported.

Batch update, with at most 500 records:

```json
{
  "action": "batch_update",
  "app_token": "S404b...",
  "table_id": "tblXXXXXXXX",
  "records": [
    {
      "record_id": "rec001",
      "fields": {
        "Status": "Complete"
      }
    },
    {
      "record_id": "rec002",
      "fields": {
        "Status": "Complete"
      }
    }
  ]
}
```

---

## Scenario 5: Create a single-select field with options

**Tool:** `feishu_bitable_app_table_field`

```json
{
  "action": "create",
  "app_token": "S404b...",
  "table_id": "tblXXXXXXXX",
  "field_name": "Priority",
  "type": 3,
  "property": {
    "options": [
      {"name": "High", "color": 0},
      {"name": "Medium", "color": 1},
      {"name": "Low", "color": 2}
    ]
  }
}
```

Example colors from the 0–54 range:

- 0: red
- 1: orange
- 10: green
- 20: blue

A multi-select field uses the same structure with `type=4`:

```json
{
  "action": "create",
  "app_token": "S404b...",
  "table_id": "tblXXXXXXXX",
  "field_name": "Tags",
  "type": 4,
  "property": {
    "options": [
      {"name": "Important", "color": 0},
      {"name": "Urgent", "color": 1},
      {"name": "Long term", "color": 10}
    ]
  }
}
```

Do not specify an option `id` during creation; Feishu generates it. A field may contain at most 20,000 options.

---

## Scenario 6: Create progress, currency, and rating fields

### Progress (`type=2`, `ui_type="Progress"`)

```json
{
  "action": "create",
  "app_token": "S404b...",
  "table_id": "tblXXXXXXXX",
  "field_name": "Completion Progress",
  "type": 2,
  "ui_type": "Progress",
  "property": {
    "min": 0,
    "max": 100,
    "range_customize": true
  }
}
```

Write `0.75` to represent 75%.

### Currency (`type=2`, `ui_type="Currency"`)

```json
{
  "action": "create",
  "app_token": "S404b...",
  "table_id": "tblXXXXXXXX",
  "field_name": "Budget",
  "type": 2,
  "ui_type": "Currency",
  "property": {
    "currency_code": "CNY",
    "formatter": "0,000.00"
  }
}
```

Common `currency_code` values:

- `"CNY"`: Chinese yuan (¥)
- `"USD"`: US dollar ($)
- `"EUR"`: euro (€)
- `"JPY"`: Japanese yen (¥)

Write an ordinary number such as `5000.50`.

### Rating (`type=2`, `ui_type="Rating"`)

```json
{
  "action": "create",
  "app_token": "S404b...",
  "table_id": "tblXXXXXXXX",
  "field_name": "Customer Satisfaction",
  "type": 2,
  "ui_type": "Rating",
  "property": {
    "min": 1,
    "max": 5,
    "rating": {
      "symbol": "star"
    }
  }
}
```

Supported symbols include:

- `"star"`: ⭐ star
- `"heart"`: ❤️ heart
- `"fire"`: 🔥 fire
- `"thumbsup"`: 👍 thumbs up

Write an integer such as `4`.

---

## Scenario 7: Work with attachments

The pinned tool inventory does not expose Bitable's dedicated media-upload
operation. Do not use a Drive `file_token` or invent a media-upload tool call:
Bitable rejects tokens that were not uploaded for the current app. If the
record needs a new attachment, explain this limitation instead of writing an
invalid value. Existing Bitable attachment tokens can still be written when
the user supplies them.

### Step 1: Create an attachment field if needed

```json
{
  "action": "create",
  "app_token": "S404b...",
  "table_id": "tblXXXXXXXX",
  "field_name": "Contract Files",
  "type": 17
}
```

### Step 2: Write the attachment record

```json
{
  "action": "create",
  "app_token": "S404b...",
  "table_id": "tblXXXXXXXX",
  "fields": {
    "Customer Name": "ByteDance",
    "Contract Files": [
      {"file_token": "DRiFxxxxxxxxxxxxxxxxxxCccoe"},
      {"file_token": "BZk3bxxxxxxxxxxxxxxxxeKqcLe"}
    ]
  }
}
```

One cell may contain at most 100 attachments. Every token must already belong
to the current Bitable app; an external `file_token` is invalid.

---

## Scenario 8: Create a bidirectional link

### Step 1: Create the bidirectional field

Create a field in the Tasks table that links to the Projects table:

```json
{
  "action": "create",
  "app_token": "S404b...",
  "table_id": "tbl_task",
  "field_name": "Project",
  "type": 21,
  "property": {
    "table_id": "tbl_project",
    "back_field_name": "Linked Tasks",
    "multiple": true
  }
}
```

Result:

- The `Project` field is created in the Tasks table.
- Feishu automatically creates the `Linked Tasks` field in the Projects table.

### Step 2: Write linked records

```json
{
  "action": "create",
  "app_token": "S404b...",
  "table_id": "tbl_task",
  "fields": {
    "Task Name": "Develop a new feature",
    "Project": {
      "link_record_ids": ["rec_project_001"]
    }
  }
}
```

Cascading update:

- The Tasks table's `Project` field is set to `rec_project_001`.
- The `Linked Tasks` field of `rec_project_001` in the Projects table automatically receives the new task's `record_id`.

### Unidirectional link (`type=18`)

A unidirectional link affects only the current table and does not update the linked table:

```jsonc
{
  "action": "create",
  "app_token": "S404b...",
  "table_id": "tbl_task",
  "field_name": "Reference Tasks",
  "type": 18,
  "property": {
    "table_id": "tbl_task", // A table may link to itself
    "multiple": true
  }
}
```

---

## 🔗 References

- [Field property configuration](field-properties.md)
- [Record value formats](record-values.md)
- [Feishu Open Platform Bitable documentation](https://open.feishu.cn/document/server-docs/docs/bitable-v1/bitable-overview)
