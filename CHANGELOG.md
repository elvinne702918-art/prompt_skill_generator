# Changelog

## 0.1.23

- Removed list defaults from file parameters because Dify tool parameter defaults only support scalar values.
- Kept runtime empty-file placeholder normalization for values that reach the plugin.

## 0.1.22

- Added explicit empty-list defaults for optional file parameters to prevent Dify workflow drafts from saving empty file slots as invalid variable references.
- Kept attachment and Skill ZIP inputs optional while making their empty state valid for Dify ToolNodeData validation.

## 0.1.21

- Upgraded the Skill registration node into a Skill management node.
- Added operations to list installed Skills and delete installed Skills.
- Made the Skill ZIP upload optional so list/delete operations do not require an attachment.

## 0.1.20

- Increased plugin storage quota to 20MB for persistent Skill presets.
- Stored registered Skill content in separate chunks so the preset list remains lightweight.
- Switched the Dify plugin icon to the packaged PNG asset from `_assets/icon.png`.

## 0.1.19

- Reduced visible attachment slots from 10 to 5 to keep the Dify node UI compact.
- Localized provider and tool labels/descriptions to Chinese.
- Kept backend compatibility for `attachment_1` through `attachment_10` in existing workflow drafts.

## 0.1.18

- Replaced multi-file tool parameters with single-file attachment slots to avoid Dify workflow `ToolNodeData` validation failures.
- Attachment execution now accepts `attachment_1` through `attachment_10`, plus existing Base64 image input.
- Changed Skill ZIP registration to a single file upload field for the same workflow compatibility reason.

## 0.1.17

- Bundled Linux Python 3.12 wheels for offline plugin dependency installation.
- Changed runtime requirements to install from local `wheels/` instead of downloading from PyPI during Dify plugin startup.
- Updated the Dify plugin SDK dependency to `dify_plugin==0.5.1` for the current plugin daemon runtime.

## 0.1.16

- Added `Base64 Images` input for Gemini inline image calls.
- Attachment execution can now run with base64 images even when Dify does not pass a `files` list.
- Base64 image inputs reuse the same `10MB` and `2048px` validation.

## 0.1.15

- Added `External API Type` selection.
- Added Google Gemini REST `generateContent` support.
- Preserved existing OpenAI-compatible `/chat/completions` behavior.
- Added `logo/` folder for repository-side logo assets.

## 0.1.14

- Added shared image validation before model invocation.
- Rejects images larger than `10MB`.
- Rejects images with width or height greater than `2048px`.
- Applies image validation to both Dify platform model calls and external OpenAI-compatible HTTP API calls.
- Added GitHub-ready documentation and ignore rules.

## 0.1.13

- Added attachment-specific execution node.
- Added persistent registered Skill ZIP presets.
- Added external API fields for OpenAI-compatible chat completion endpoints.
