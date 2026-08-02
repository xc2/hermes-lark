# Feishu Bitable field property reference

This document describes the `property` structure required when creating or updating each field type.

> **Source:** Feishu Open Platform [Field editing guide](https://go.feishu.cn/s/672BSzVyo03)

## 📋 Contents

- [Basic fields](#basic-fields)
  - [1. Text (`type=1`)](#1-text-type1)
  - [2. Number (`type=2`)](#2-number-type2)
  - [5. Date and time (`type=5`)](#5-date-and-time-type5)
  - [7. Checkbox (`type=7`)](#7-checkbox-type7)
  - [13. Phone number (`type=13`)](#13-phone-number-type13)
- [Select fields](#select-fields)
  - [3. Single select (`type=3`)](#3-single-select-type3)
  - [4. Multi-select (`type=4`)](#4-multi-select-type4)
- [Special display fields](#special-display-fields)
  - [Progress (`type=2`, `ui_type="Progress"`)](#progress-type2-ui_typeprogress)
  - [Currency (`type=2`, `ui_type="Currency"`)](#currency-type2-ui_typecurrency)
  - [Rating (`type=2`, `ui_type="Rating"`)](#rating-type2-ui_typerating)
  - [Barcode (`type=1`, `ui_type="Barcode"`)](#barcode-type1-ui_typebarcode)
  - [Email (`type=1`, `ui_type="Email"`)](#email-type1-ui_typeemail)
- [Relationship fields](#relationship-fields)
  - [11. User (`type=11`)](#11-user-type11)
  - [15. URL (`type=15`)](#15-url-type15)
  - [17. Attachment (`type=17`)](#17-attachment-type17)
  - [18. Unidirectional link (`type=18`)](#18-unidirectional-link-type18)
  - [21. Bidirectional link (`type=21`)](#21-bidirectional-link-type21)
  - [22. Location (`type=22`)](#22-location-type22)
  - [23. Group chat (`type=23`)](#23-group-chat-type23)
- [Advanced fields](#advanced-fields)
  - [20. Formula (`type=20`)](#20-formula-type20)
  - [1001. Created time (`type=1001`)](#1001-created-time-type1001)
  - [1002. Last modified time (`type=1002`)](#1002-last-modified-time-type1002)
  - [1005. Autonumber (`type=1005`)](#1005-autonumber-type1005)

---

## Basic fields

### 1. Text (`type=1`)

**Property structure:** An empty object, or omit it.

```json
{
  "type": 1,
  "field_name": "Task Description",
  "property": {}
}
```

Notes:

- The default `ui_type` is `"Text"`.
- One cell may contain at most 100,000 characters.
- Rich text, including mentions and links, is supported.

---

### 2. Number (`type=2`)

**Property structure:**

```jsonc
{
  "formatter": "0" // Optional number-display format
}
```

Supported `formatter` values:

- `"0"`: integer (default)
- `"0.0"`: one decimal place
- `"0.00"`: two decimal places
- `"0,000"`: thousands separators
- `"0.00%"`: percentage

Example:

```json
{
  "type": 2,
  "field_name": "Hours",
  "property": {
    "formatter": "0.00"
  }
}
```

---

### 5. Date and time (`type=5`)

**Property structure:**

```jsonc
{
  "date_formatter": "yyyy/MM/dd", // Optional; defaults to "yyyy/MM/dd"
  "auto_fill": false               // Optional; fill with the creation time
}
```

Supported `date_formatter` values include:

- `"yyyy/MM/dd"`: 2021/1/30
- `"yyyy-MM-dd HH:mm"`: 2021/1/30 14:00
- `"MM-dd"`: Jan 30
- `"MM/dd/yyyy"`: 01/30/2021
- `"dd/MM/yyyy"`: 30/01/2021

Example:

```json
{
  "type": 5,
  "field_name": "Due Date",
  "property": {
    "date_formatter": "yyyy-MM-dd HH:mm",
    "auto_fill": false
  }
}
```

---

### 7. Checkbox (`type=7`)

**Property structure:** An empty object, or omit it.

```json
{
  "type": 7,
  "field_name": "Complete",
  "property": {}
}
```

---

### 13. Phone number (`type=13`)

**Property structure:** An empty object, or omit it.

```json
{
  "type": 13,
  "field_name": "Contact Number",
  "property": {}
}
```

Notes:

- Phone numbers must match `(\+)?\d*`.
- The maximum length is 64 characters.

---

## Select fields

### 3. Single select (`type=3`)

**Property structure:**

```jsonc
{
  "options": [
    {
      "name": "In progress", // Required option name
      "color": 0              // Optional color number from 0 through 54
    },
    {
      "name": "Complete",
      "color": 10
    }
  ]
}
```

Color numbers:

- Range: 0–54
- 0: red
- 10: green
- 20: blue
- See the Feishu documentation for the complete palette.

Example:

```json
{
  "type": 3,
  "field_name": "Task Status",
  "property": {
    "options": [
      {"name": "Not started", "color": 0},
      {"name": "In progress", "color": 20},
      {"name": "Complete", "color": 10}
    ]
  }
}
```

Notes:

- A field may contain at most 20,000 options.
- Do not specify an option `id` when creating an option; the system generates it.
- Preserve the `id` of every existing option when updating the field.

---

### 4. Multi-select (`type=4`)

**Property structure:** The same as single select.

```json
{
  "options": [
    {"name": "Urgent", "color": 0},
    {"name": "Important", "color": 10}
  ]
}
```

Notes:

- A field may contain at most 20,000 options.
- One cell may select at most 1,000 options.

---

## Special display fields

### Progress (`type=2`, `ui_type="Progress"`)

**Property structure:**

```jsonc
{
  "min": 0,                  // Required minimum
  "max": 100,                // Required maximum
  "range_customize": false   // Optional; permit values outside the range
}
```

Example:

```json
{
  "type": 2,
  "field_name": "Progress",
  "ui_type": "Progress",
  "property": {
    "min": 0,
    "max": 100,
    "range_customize": true
  }
}
```

Notes:

- `min` must be between 0 and 1.
- `max` must be between 1 and 100.
- When `range_customize` is `true`, users may enter values outside the configured range.

---

### Currency (`type=2`, `ui_type="Currency"`)

**Property structure:**

```jsonc
{
  "currency_code": "CNY", // Required currency
  "formatter": "0.00"     // Optional number format
}
```

Common `currency_code` values:

- `"CNY"`: Chinese yuan (¥)
- `"USD"`: US dollar ($)
- `"EUR"`: euro (€)
- `"GBP"`: pound sterling (£)
- `"JPY"`: Japanese yen (¥)
- `"HKD"`: Hong Kong dollar ($)
- More than 20 currencies are supported.

Example:

```json
{
  "type": 2,
  "field_name": "Budget",
  "ui_type": "Currency",
  "property": {
    "currency_code": "USD",
    "formatter": "0,000.00"
  }
}
```

---

### Rating (`type=2`, `ui_type="Rating"`)

**Property structure:**

```jsonc
{
  "min": 1,           // Required minimum
  "max": 5,           // Required maximum
  "rating": {         // Optional display style
    "symbol": "star" // Icon type
  }
}
```

Supported `symbol` values:

- `"star"`: ⭐ star (default)
- `"heart"`: ❤️ heart
- `"thumbsup"`: 👍 thumbs up
- `"fire"`: 🔥 fire
- `"smile"`: 😊 smile
- `"lightning"`: ⚡ lightning
- `"flower"`: 🌸 flower
- `"number"`: number

Example:

```json
{
  "type": 2,
  "field_name": "Priority",
  "ui_type": "Rating",
  "property": {
    "min": 1,
    "max": 5,
    "rating": {
      "symbol": "fire"
    }
  }
}
```

---

### Barcode (`type=1`, `ui_type="Barcode"`)

**Property structure:**

```jsonc
{
  "allowed_edit_modes": {
    "manual": true, // Permit manual entry
    "scan": true    // Permit scanning
  }
}
```

Example:

```json
{
  "type": 1,
  "field_name": "Product Barcode",
  "ui_type": "Barcode",
  "property": {
    "allowed_edit_modes": {
      "manual": false,
      "scan": true
    }
  }
}
```

---

### Email (`type=1`, `ui_type="Email"`)

**Property structure:** An empty object, or omit it.

```json
{
  "type": 1,
  "field_name": "Contact Email",
  "ui_type": "Email",
  "property": {}
}
```

---

## Relationship fields

### 11. User (`type=11`)

**Property structure:**

```jsonc
{
  "multiple": true // Optional; defaults to true
}
```

Example:

```jsonc
{
  "type": 11,
  "field_name": "Owner",
  "property": {
    "multiple": false // Permit only one user
  }
}
```

Notes:

- One cell may contain at most 1,000 users.
- Record values support only the `id` property, using an `open_id`, `union_id`, or `user_id`.

---

### 15. URL (`type=15`)

**Property structure:** Omit the `property` parameter completely. Do not pass any value, including an empty object.

```jsonc
{
  "type": 15,
  "field_name": "Reference Link"
  // Do not include property, including property: {}
}
```

This is a special Feishu API requirement confirmed in live testing:

- ✅ Correct: omit `property` completely.
- ❌ Incorrect: `"property": {}`; this causes `URLFieldPropertyError`.
- ❌ Incorrect: pass any other `property` value.

---

### 17. Attachment (`type=17`)

**Property structure:** An empty object, or omit it.

```json
{
  "type": 17,
  "field_name": "Attachments",
  "property": {}
}
```

Notes:

- One cell may contain at most 100 attachments.
- Upload files through the [media upload API](https://go.feishu.cn/s/63soQp6O80s) before writing the record.

---

### 18. Unidirectional link (`type=18`)

**Property structure:**

```jsonc
{
  "table_id": "tblXXXXXXXX", // Required linked table ID
  "multiple": true            // Optional; defaults to true
}
```

Example:

```json
{
  "type": 18,
  "field_name": "Linked Tasks",
  "property": {
    "table_id": "tblsRc9GRRXKqhvW",
    "multiple": true
  }
}
```

One cell may link at most 500 records.

---

### 21. Bidirectional link (`type=21`)

**Property structure:**

```jsonc
{
  "table_id": "tblXXXXXXXX",        // Required linked table ID
  "back_field_name": "Reverse Link", // Required field name in the other table
  "multiple": true                    // Optional; permit multiple records
}
```

Example:

```json
{
  "type": 21,
  "field_name": "Related Projects",
  "property": {
    "table_id": "tblAnotherTable",
    "back_field_name": "Linked Tasks",
    "multiple": true
  }
}
```

Notes:

- One cell may link at most 500 records.
- Feishu automatically creates the corresponding bidirectional field in the other table.

---

### 22. Location (`type=22`)

**Property structure:**

```jsonc
{
  "location": {
    "input_type": "not_limit" // Input restriction
  }
}
```

Supported `input_type` values:

- `"only_mobile"`: require live location from a mobile device
- `"not_limit"`: no restriction (default)

Example:

```json
{
  "type": 22,
  "field_name": "Office Address",
  "property": {
    "location": {
      "input_type": "only_mobile"
    }
  }
}
```

---

### 23. Group chat (`type=23`)

**Property structure:** An empty object, or omit it.

```json
{
  "type": 23,
  "field_name": "Collaboration Group",
  "property": {}
}
```

One cell may contain at most 10 group chats.

---

## Advanced fields

### 20. Formula (`type=20`)

**Property structure:**

```jsonc
{
  "formula_expression": "bitable::$table[tblXXX].$field[fldYYY]*2" // Optional
}
```

Example:

```json
{
  "type": 20,
  "field_name": "Total Price",
  "property": {
    "formula_expression": "bitable::$table[tblMain].$field[fldQty] * $field[fldPrice]"
  }
}
```

Notes:

- Setting the formula expression is unsupported while creating a field.
- See the [Feishu Help Center formula field documentation](https://www.feishu.cn/hc/zh-CN/articles/360049067853).

For some Bitable apps, formula fields require an additional `type` property. Determine this from `formula_type` returned by the [Bitable metadata API](https://go.feishu.cn/s/62nuKkQlE03):

```jsonc
{
  "type": 20,
  "field_name": "Calculated Value",
  "property": {
    "type": {
      "data_type": 2,          // Formula result type: 1=text, 2=number, 5=date, and so on
      "ui_property": {         // Display properties
        "formatter": "0.00",
        "currency_code": "CNY"
      },
      "ui_type": "Currency"   // Number, Progress, Currency, Rating, or DateTime
    }
  }
}
```

---

### 1001. Created time (`type=1001`)

**Property structure:**

```jsonc
{
  "date_formatter": "yyyy/MM/dd" // Optional date format
}
```

Example:

```json
{
  "type": 1001,
  "field_name": "Created At",
  "property": {
    "date_formatter": "yyyy-MM-dd HH:mm"
  }
}
```

---

### 1002. Last modified time (`type=1002`)

**Property structure:** The same as created time.

```json
{
  "date_formatter": "yyyy-MM-dd HH:mm"
}
```

---

### 1005. Autonumber (`type=1005`)

**Property structure:**

```jsonc
{
  "auto_serial": {
    "type": "auto_increment_number", // Or "custom"
    "options": [                      // Required only when type is "custom"
      {
        "type": "fixed_text",
        "value": "TASK-"
      },
      {
        "type": "created_time",
        "value": "yyyyMMdd"
      },
      {
        "type": "system_number",
        "value": "5"
      }
    ]
  }
}
```

Supported `auto_serial.type` values:

- `"auto_increment_number"`: a plain incrementing number
- `"custom"`: a custom numbering rule

Rule types in `options`:

- `"system_number"`: number of digits in the incrementing counter; `value` is 1–9
- `"fixed_text"`: fixed text; `value` is at most 20 characters
- `"created_time"`: creation date; `value` is `"yyyyMMdd"`, `"yyyyMM"`, `"yyyy"`, `"MMdd"`, `"MM"`, or `"dd"`

Example 1, plain incrementing number:

```json
{
  "type": 1005,
  "field_name": "Number",
  "property": {
    "auto_serial": {
      "type": "auto_increment_number"
    }
  }
}
```

Example 2, custom autonumber:

```jsonc
{
  "type": 1005,
  "field_name": "Work Order Number",
  "property": {
    "auto_serial": {
      "type": "custom",
      "options": [
        {"type": "fixed_text", "value": "WO-"},
        {"type": "created_time", "value": "yyyyMMdd"},
        {"type": "system_number", "value": "4"}
      ]
    }
  }
}
// Example result: WO-20240226-0001
```

---

## 🔍 Common error codes

| Error code | Field type | Meaning |
| --- | --- | --- |
| `1254080` | Text | Invalid `property` structure |
| `1254081` | Number | Invalid `property`; check `formatter` |
| `1254082` | Single select | Invalid `property`; check the `options` array |
| `1254083` | Multi-select | Invalid `property`; check the `options` array |
| `1254084` | Date and time | Invalid `property`; check `date_formatter` |
| `1254085` | Checkbox | Invalid `property` structure |
| `1254086` | User | Invalid `property`; check `multiple` |
| `1254087` | URL | Omit `property` completely; even an empty object fails |
| `1254088` | Attachment | Invalid `property` structure |
| `1254089` | Unidirectional link | Invalid `property`; check `table_id` |
| `1254090` | Lookup | Invalid `property` structure |
| `1254091` | Formula | Invalid `property` structure |
| `1254092` | Bidirectional link | Invalid `property`; check `table_id` and `back_field_name` |
| `1254093` | Created time | Invalid `property` structure |
| `1254094` | Last modified time | Invalid `property` structure |

---

## 📌 Special rules when updating fields

When using the `update` action:

1. Keep the field type unchanged: `type` and `ui_type` cannot be changed.
2. When updating single-select or multi-select options:
   - Preserve the `id` of every existing option.
   - For a new option, send only `name` and `color`; omit `id`.
3. To rename a field only, send `field_name`; the tool automatically retrieves the current `type` and `property`.
4. A linked field's `table_id` cannot be changed to a different table.

---

## 🔗 References

- [Feishu Open Platform field editing guide](https://go.feishu.cn/s/672BSzVyo03)
- [Create field API](https://go.feishu.cn/s/62nuKkQl403)
- [Update field API](https://go.feishu.cn/s/62nuKkQlo03)
