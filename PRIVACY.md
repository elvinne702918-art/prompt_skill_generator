# Privacy Policy

This plugin processes user-provided skill instructions and task inputs inside the configured Dify plugin runtime.

## Data Processed

The plugin may process:

- text entered into the node
- uploaded Skill ZIP packages
- text files and documents attached to the attachment node
- images attached to the attachment node
- external API key and base URL values entered in node configuration

## Storage

Registered skill presets are saved in Dify plugin storage so they can be reused by later workflow runs and other nodes in the same Dify installation.

The plugin does not intentionally persist uploaded task attachments outside Dify's normal file/plugin runtime handling.

## External Transmission

When an External API Key is provided, prompts and readable attachment content may be sent to the configured external API endpoint, including OpenAI-compatible endpoints or Google Gemini.

When no External API Key is provided, model calls are routed through the Dify platform model configured in the node.

## Security Limits

Images are rejected before model invocation if they exceed:

- `10MB`
- `2048px` on either width or height

Skill ZIP package text extraction is limited and only safe relative paths are read.

## Contact

For private deployments, contact the operator who installed this plugin.
