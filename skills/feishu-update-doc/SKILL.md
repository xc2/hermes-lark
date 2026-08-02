---
name: feishu-update-doc
description: |
  Update Feishu cloud documents with seven modes: append, overwrite, replace a selected range, replace every match, insert before, insert after, or delete a range.
---

# feishu_update_doc

Update a Feishu cloud document with one of seven modes. Prefer targeted operations such as `replace_range`, `append`, `insert_before`, or `insert_after`. Use `overwrite` cautiously because it clears and rebuilds the document and may lose images, comments, or other content.

# Selection methods

The selection-based modes—`replace_range`, `replace_all`, `insert_before`, `insert_after`, and `delete_range`—accept exactly one of the following selection methods.

## `selection_with_ellipsis`: select by content

Two formats are supported:

1. **Range match:** `starting content...ending content`
   - Matches everything from the start through the end, including the boundary text.
   - Use 10–20 characters of context at each boundary when possible to make the match unique.
2. **Exact match:** `complete content`, without `...`
   - Matches that complete text.
   - Useful for a short phrase, keyword, or other exact text.

If the target text literally contains `...`, escape it as `\.\.\.`.

Examples:

- `Hello...world` matches from `Hello` through `world`, including any content between them.
- `Hello\.\.\.world` matches the literal text `Hello...world`.

If the document contains several occurrences of the boundary text, include more surrounding context to avoid ambiguity.

## `selection_by_title`: select a section by heading

Use a heading such as `## Feature Overview`; the `#` prefix is optional.

The tool selects the entire section from that heading up to, but not including, the next heading of the same or a higher level.

Examples:

- `## Feature Overview` selects the level-two heading and all content in its section.
- `Feature Overview` selects a heading with that title at any level and its section.

# Optional parameters

## `new_title`

Set `new_title` to update the document title after the content update succeeds.

Rules:

- Plain text only; rich text is unsupported.
- Length must be 1–800 characters.
- May be combined with any update mode.
- The title is updated after the document content.

# Return values

## Success

```json
{
  "success": true,
  "doc_id": "document-id",
  "mode": "selected-mode",
  "message": "Document updated successfully",
  "warnings": ["optional warning"],
  "log_id": "request-log-id"
}
```

## Asynchronous processing for a large document

```json
{
  "task_id": "async_task_xxxx",
  "message": "The document update was submitted for asynchronous processing; query it with task_id",
  "log_id": "request-log-id"
}
```

Call `update-doc` again with only the returned `task_id` to query its status.

## Error

```json
{
  "error": "[error code] Error message\n💡 Suggestion: recommended fix\n📍 Context: contextual information",
  "log_id": "request-log-id"
}
```

---

# Examples

## `append`: append to the end

```json
{
  "doc_id": "document-id-or-url",
  "mode": "append",
  "markdown": "## New Section\n\nAppended content..."
}
```

## `replace_range`: replace one selected range

Using `selection_with_ellipsis`:

```json
{
  "doc_id": "document-id-or-url",
  "mode": "replace_range",
  "selection_with_ellipsis": "## Old Section...End of the old section.",
  "markdown": "## New Section\n\nNew content..."
}
```

Using `selection_by_title` to replace a complete section:

```json
{
  "doc_id": "document-id-or-url",
  "mode": "replace_range",
  "selection_by_title": "## Feature Overview",
  "markdown": "## Feature Overview\n\nUpdated feature overview..."
}
```

## `replace_all`: replace every match

This mode resembles `replace_range`, but it may replace several matches; `replace_range` requires a unique match.

```json
{
  "doc_id": "document-id-or-url",
  "mode": "replace_all",
  "selection_with_ellipsis": "Alice",
  "markdown": "Bob"
}
```

The response includes `replace_count`:

```json
{
  "success": true,
  "replace_count": 4,
  "message": "Document updated successfully with four replacements"
}
```

Notes:

- Unlike `replace_range`, `replace_all` permits multiple matches.
- An error is returned when no content matches.
- `markdown` may be an empty string to delete every matching occurrence.

## `insert_before`: insert before a selection

```json
{
  "doc_id": "document-id-or-url",
  "mode": "insert_before",
  "selection_with_ellipsis": "## Dangerous Operation...Risk of data loss.",
  "markdown": "> **Warning:** Proceed with caution."
}
```

## `insert_after`: insert after a selection

```json
{
  "doc_id": "document-id-or-url",
  "mode": "insert_after",
  "selection_with_ellipsis": "```python...```",
  "markdown": "**Example output:**\n```\nresult = 42\n```"
}
```

## `delete_range`: delete selected content

Using `selection_with_ellipsis`:

```json
{
  "doc_id": "document-id-or-url",
  "mode": "delete_range",
  "selection_with_ellipsis": "## Deprecated Section...Content that is no longer needed."
}
```

Using `selection_by_title` to delete a complete section:

```json
{
  "doc_id": "document-id-or-url",
  "mode": "delete_range",
  "selection_by_title": "## Deprecated Section"
}
```

`delete_range` does not accept a `markdown` parameter.

## Update the title and content together

Add `new_title` to any update mode:

```json
{
  "doc_id": "document-id-or-url",
  "mode": "overwrite",
  "markdown": "# Project Documentation v2.0\n\nCompletely new content...",
  "new_title": "Project Documentation v2.0"
}
```

```json
{
  "doc_id": "document-id-or-url",
  "mode": "append",
  "markdown": "## Changelog\n\n2025-12-18: Added a new feature...",
  "new_title": "Project Documentation (Updated)"
}
```

## `overwrite`: replace the entire document

This mode clears and rewrites the document. It may lose images, comments, and other content; use it only when the document must be rebuilt completely.

```json
{
  "doc_id": "document-id-or-url",
  "mode": "overwrite",
  "markdown": "# New Document\n\nCompletely new content..."
}
```

---

# Best practices

## Make the smallest precise replacement

A smaller selection is safer. This is particularly important for tables, columns, and other nested blocks: select only the text that must change so surrounding content remains intact.

For example, when a table cell contains both an image and text:

- ❌ Replacing the entire table or row may break the image reference.
- ✅ Selecting only the text leaves the image and other content unchanged.

## Protect content that cannot be reconstructed

Images, whiteboards, spreadsheets, Bitables, tasks, and similar resources are stored as tokens. They cannot be read and written back in an identical form.

- Avoid selecting ranges that contain these resources.
- Target a plain-text portion precisely.

## Prefer incremental updates to a full overwrite

When changing several places:

- ✅ Apply several small, targeted replacements.
- ⚠️ Use `overwrite` only when the risk of rebuilding the whole document is acceptable.

Local updates preserve media, comments, and collaboration history.

## Preserve the intended insertion boundary when widening a selection

When the target for `insert_before` or `insert_after` is repeated, widen `selection_with_ellipsis` until it is unique.

The insertion point is determined by the selected range's boundary:

- `insert_after` inserts after the end of the selected range.
- `insert_before` inserts before the start of the selected range.

When widening the range, keep the relevant boundary at the intended insertion point.

## Repair invalid whiteboard syntax

When `create-doc` or `update-doc` returns a warning that a whiteboard could not be written:

1. Find the whiteboard tag in the warning, for example `<whiteboard token="xxx"/>`.
2. Inspect the error and correct the Mermaid or PlantUML syntax.
3. Call `replace_range` with the warning's whiteboard tag as `selection_with_ellipsis` and the corrected code block as `markdown`.
4. Submit the update and verify it again.

---

# Notes

- **Markdown:** Feishu extensions are supported; see the `create-doc` tool documentation.
