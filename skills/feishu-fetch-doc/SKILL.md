---
name: feishu-fetch-doc
description: |
  Fetch Feishu cloud-document content as Markdown. Images, files, and whiteboards can be downloaded with the feishu_doc_media tool.
---

# feishu_fetch_doc

Fetch a Feishu cloud document as Lark-flavored Markdown.

## Important: Images, Files, and Whiteboards

**Images, files, and whiteboards embedded in a document must be fetched separately with `feishu_doc_media` using the `download` action.**

### Recognized formats

The returned Markdown represents media as HTML tags.

- **Image**:

  ```html
  <image token="Z1FjxxxxxxxxxxxxxxxxxxxtnAc" width="1833" height="2491" align="center"/>
  ```

- **File**:

  ```html
  <view type="1">
    <file token="Z1FjxxxxxxxxxxxxxxxxxxxtnAc" name="skills.zip"/>
  </view>
  ```

- **Whiteboard**:

  ```html
  <whiteboard token="Z1FjxxxxxxxxxxxxxxxxxxxtnAc"/>
  ```

### Download procedure

1. Extract the `token` attribute from the HTML tag.
2. Use `feishu_doc_media` to download the resource:

   ```json
   {
     "action": "download",
     "resource_token": "extracted-token",
     "resource_type": "media",
     "output_path": "/path/to/save/file"
   }
   ```

## Parameters

- **`doc_id`** (required): Accepts either a document URL or a token.
  - Document URL: `https://xxx.feishu.cn/docx/Z1FjxxxxxxxxxxxxxxxxxxxtnAc`; the tool extracts the token automatically.
  - Document token: `Z1FjxxxxxxxxxxxxxxxxxxxtnAc`
  - Wiki URLs and tokens are also accepted, for example `https://xxx.feishu.cn/wiki/Z1FjxxxxxxxxxxxxxxxxxxxtnAc` or `Z1FjxxxxxxxxxxxxxxxxxxxtnAc`.

## Wiki URL Resolution

A wiki link in the form `/wiki/TOKEN` can point to a cloud document, spreadsheet, Bitable app, or another object type. When the type is unknown, **do not assume it is a cloud document**. Resolve the actual type first.

### Procedure

1. Use `feishu_wiki_space_node` with the `get` action to resolve the wiki token:

   ```json
   { "action": "get", "token": "wiki_token_here" }
   ```

2. Read `obj_type`, the actual object type, and `obj_token`, the actual object token, from the returned `node`.
3. Choose the tool based on `obj_type`:

| `obj_type` | Tool | Parameter |
| --- | --- | --- |
| `docx` | `feishu_fetch_doc` | `doc_id = obj_token` |
| `sheet` | `feishu_sheet` | `spreadsheet_token = obj_token` |
| `bitable` | `feishu_bitable_*` tool family | `app_token = obj_token` |
| Other | Tell the user that the object type is not currently supported | - |

### Example

User request: `Please review this document: https://xxx.feishu.cn/wiki/ABC123`

1. Call `feishu_wiki_space_node` with `action: "get"` and `token: "ABC123"`.
2. Receive `obj_type: "docx"` and `obj_token: "doxcnXYZ789"`.
3. Call `feishu_fetch_doc` with `doc_id: "doxcnXYZ789"`.

## Related Tools

| Need | Tool |
| --- | --- |
| Fetch document text | `feishu_fetch_doc` |
| Download an image, file, or whiteboard | `feishu_doc_media` with `action: "download"` |
| Resolve a wiki token's object type | `feishu_wiki_space_node` with `action: "get"` |
| Read or write a spreadsheet | `feishu_sheet` |
| Work with Bitable | `feishu_bitable_*` tool family |
