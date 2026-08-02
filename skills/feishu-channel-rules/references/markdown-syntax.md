# Feishu Markdown syntax reference

> This file is a complete reference for the Markdown syntax supported by Feishu message cards. Consult it when needed.

## 1. Headings

```markdown
#### Level-four heading
##### Level-five heading
```

- Level-one through level-three headings (`#`, `##`, and `###`) are unsupported and may render incorrectly in a card.
- Use bold text when a larger heading level is needed.

## 2. Line breaks

```text
First line\nSecond line
```

## 3. Text styles

| Syntax | Result |
| --- | --- |
| `**bold**` | **bold** |
| `*italic*` | *italic* |
| `~~strikethrough~~` | ~~strikethrough~~ |

> **Note:** Bold content may contain Chinese or English text, but not CJK punctuation or emoji.

## 4. Markdown links

```markdown
[Link text](https://www.example.com)
```

## 5. Mention specific users

```html
<at id=id_01></at>
<at ids=id_01,id_02,xxx></at>
```

- Use only an ID supplied by the user; never invent one.
- A valid identifier may be a string beginning with `ou_`, a string of at most 10 characters, or an email address.

## 6. Empty HTML link

```html
<a href='https://open.feishu.cn'></a>
```

## 7. Colored text

```html
<font color='green'>Green text</font>
```

Supported colors: `neutral`, `blue`, `turquoise`, `lime`, `orange`, `violet`, `wathet`, `green`, `yellow`, `red`, `purple`, and `carmine`.

## 8. HTML text link

```html
<a href='https://open.feishu.cn'>Text link</a>
```

## 9. Images

```markdown
![hover_text](image_key)
```

An `image_key` cannot be an HTTP URL.

## 10. Horizontal rule

```markdown
---
```

## 11. Text tags

```html
<text_tag color='red'>Tag text</text_tag>
```

Supported colors: `neutral`, `blue`, `turquoise`, `lime`, `orange`, `violet`, `wathet`, `green`, `yellow`, `red`, `purple`, and `carmine`.

## 12. Ordered lists

```text
1. First top-level item
    1.1 First nested item
    1.2 Second nested item
2. Second top-level item
```

- Put the number at the start of the line and add a space after it.
- Four spaces represent one indentation level.

## 13. Unordered lists

```text
- First top-level item
    - Nested item
- Second top-level item
```

- Four spaces represent one indentation level.
- Add a space after `-`.

## 14. Code blocks

````markdown
```JSON
{"This is": "a JSON example"}
```
````

- A language may be specified for syntax highlighting.
- Without a language, the content is rendered as plain text.

## 15. Person component

```html
<person id='user_id' show_name=true show_avatar=true style='normal'></person>
```

- `show_name`: whether to show the user's name; defaults to `true`
- `show_avatar`: whether to show the user's avatar; defaults to `true`
- `style`: `normal` for the standard style or `capsule` for the capsule style
- A `person` tag cannot be nested inside a `font` tag.

## 16. Number badge

```html
<number_tag background_color='grey' font_color='white' url='https://open.feishu.cn' pc_url='https://open.feishu.cn' android_url='https://open.feishu.cn' ios_url='https://open.feishu.cn'>1</number_tag>
```
