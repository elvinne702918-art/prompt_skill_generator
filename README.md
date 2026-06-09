# Prompt Skill Generator for Dify

Prompt Skill Generator is a Dify plugin for running reusable prompt skills. It supports built-in templates, pasted custom skill content, uploaded Skill ZIP packages, persistent registered Skill presets, and attachment-aware execution with text files, images, and documents.

Read this in Chinese: [简体中文](README.zh-Hans.md)

## Features

- Execute a selected skill directly, returning the final content.
- Generate a reusable prompt from a selected skill.
- Register a Skill ZIP package once and reuse it from other nodes.
- Accept attachments in a dedicated attachment node.
- Support Dify platform LLM models and OpenAI-compatible external HTTP APIs.
- Hide common reasoning blocks such as `<think>...</think>` by default.
- Reject oversized images before model invocation.

## Tools

### Register Skill Package

Uploads one ZIP package and saves it as a reusable Skill preset in Dify plugin storage.

The ZIP package must contain a `SKILL.md` file. Text files under a `references/` directory are included as supporting skill material.

### Generate Prompt

Runs a built-in, custom, uploaded, or registered skill without requiring attachments.

### Generate Prompt With Attachments

Runs a skill with attachments or base64 image input. Use `Attachment 1` through `Attachment 5` for uploaded files. Existing workflows that already contain `attachment_6` through `attachment_10` remain readable by the backend. Supported input types include:

- text files
- one or more images
- documents
- base64 image data URLs or raw base64 images

Image limits are enforced before calling the model:

- maximum image size: `10MB`
- maximum image width or height: `2048px`

The same validation is applied to Dify platform model calls and external HTTP API calls.

## External API Mode

If `External API Key` is provided, the plugin uses `External API Type`.

For `OpenAI Compatible`, the plugin calls:

```text
POST {api_base}/chat/completions
Authorization: Bearer <api_key>
```

If `External API Base URL` is empty, the default is:

```text
https://api.openai.com/v1
```

For `Google Gemini`, the plugin calls:

```text
POST {api_base}/models/{model}:generateContent
x-goog-api-key: <api_key>
```

If `External API Base URL` is empty, the Google Gemini default is:

```text
https://generativelanguage.googleapis.com/v1beta
```

The selected Dify model name is reused as the external `model` field. For custom providers, make sure the selected model name matches the remote API.

Use `External Model Name` to override the model sent to the external API. This is useful for Google Gemini because the Dify model selector may not contain a `gemini-*` model name.

For Gemini image input, you can use the `Base64 Images` field instead of Dify file attachments. Paste one data URL or raw base64 image per line:

```text
data:image/png;base64,iVBORw0KGgo...
```

JSON arrays are also supported:

```json
[
  {
    "filename": "product.png",
    "mime_type": "image/png",
    "base64": "iVBORw0KGgo..."
  }
]
```

## Local Development

Install dependencies:

```bash
pip install -r requirements-dev.txt
```

`requirements-dev.txt` installs test dependencies from the Python package index. `requirements.txt` is reserved for Dify runtime packaging and expects a local `wheels/` directory.

Run tests:

```bash
python -m unittest tests.test_generate_prompt
```

## Offline Runtime Dependencies

Release packages can include Linux x86_64 Python 3.12 wheels under `wheels/`. `requirements.txt` installs from that local folder so Dify does not need to download runtime dependencies while starting the plugin. The source repository does not commit wheel files; regenerate the wheelhouse before packaging if it is missing. This package is declared as `amd64` in `manifest.yaml`.

Refresh the wheelhouse only when runtime dependencies change:

```bash
python -m pip download -d wheels -i https://pypi.tuna.tsinghua.edu.cn/simple --only-binary=:all: --platform manylinux2014_x86_64 --python-version 312 --implementation cp --abi cp312 dify_plugin==0.5.1 httpx==0.28.1
```

Package with the official Dify plugin tooling for Marketplace or signed distribution. The local helper package script in this workspace is only for private testing and does not create a trusted Marketplace signature.

## Private Installation Note

For private `.difypkg` distribution on self-hosted Dify, target servers with forced signature verification enabled may reject local packages. Either publish through the official Marketplace flow, configure third-party signature verification, or disable forced signature verification only in trusted self-hosted environments.

## Publishing Checklist

- Replace `author: user` in `manifest.yaml` and provider/tool manifests with your GitHub username or organization.
- Review `PRIVACY.md` and update the contact/owner information.
- Run the unit tests.
- Package with official Dify tooling.
- Do not commit generated `.difypkg` files or `__pycache__` folders.

## Logo Assets

Put brand assets under `logo/` when you want a dedicated folder for repository-side logo files. Keep the plugin icon used by Dify in `_assets/`.
