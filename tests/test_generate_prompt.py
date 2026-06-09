import base64
import io
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import yaml

from tools.generate_prompt.generate_prompt import (
    SKILL_CONTENT_CHUNK_PREFIX,
    SKILL_REGISTRY_KEY,
    build_google_generate_content_payload,
    call_external_llm,
    build_execution_prompt,
    build_template_options,
    build_template_runtime_parameter,
    build_prompt,
    build_external_messages,
    build_user_prompt_message,
    compose_skill_from_sources,
    determine_external_api_type,
    determine_output_mode,
    extract_skill_package,
    get_tool_input_attachments,
    get_tool_input_files,
    get_registered_skill,
    list_registered_skills,
    load_skill_registry,
    normalize_files,
    render_template_variables,
    save_registered_skill,
    delete_registered_skill,
    select_skill_content,
    resolve_skill_content,
    strip_reasoning_content,
)


class FakeStorage:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values[key]

    def set(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


class LimitedValueStorage(FakeStorage):
    def __init__(self, max_value_bytes):
        super().__init__()
        self.max_value_bytes = max_value_bytes

    def set(self, key, value):
        if len(value) > self.max_value_bytes:
            raise ValueError("allocated size is greater than max storage size")
        super().set(key, value)


class FakeStreamResponse:
    def __init__(self, content: bytes, headers: dict[str, str] | None = None):
        self.content = content
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self, chunk_size=65536):
        for index in range(0, len(self.content), chunk_size):
            yield self.content[index : index + chunk_size]


class FakePostResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeHttpClient:
    last_post: dict | None = None

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url, json=None, headers=None):
        FakeHttpClient.last_post = {"url": url, "json": json, "headers": headers}
        return FakePostResponse(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "Gemini result"},
                            ]
                        }
                    }
                ]
            }
        )


def make_png_header(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )


class SkillContentTests(unittest.TestCase):
    def test_resolves_markdown_skill_frontmatter(self):
        raw_skill = """---
name: code-review
description: Review code with a strict findings-first format.
---

# Code Review

Find bugs first.
Return findings ordered by severity.
"""

        resolved = resolve_skill_content(raw_skill)

        self.assertIn("code-review", resolved)
        self.assertIn("Review code with a strict findings-first format.", resolved)
        self.assertIn("Find bugs first.", resolved)
        self.assertIn("Return findings ordered by severity.", resolved)

    def test_resolves_json_skill_object(self):
        raw_skill = """{
  "name": "translation",
  "description": "Translate with terminology consistency.",
  "skill": "Keep domain terms consistent and preserve tone."
}"""

        resolved = resolve_skill_content(raw_skill)

        self.assertIn("translation", resolved)
        self.assertIn("Translate with terminology consistency.", resolved)
        self.assertIn("Keep domain terms consistent and preserve tone.", resolved)

    def test_resolves_localized_builtin_template_fields(self):
        raw_skill = {
            "id": "translate",
            "name": {"en_US": "Translation", "zh_Hans": "翻译"},
            "description": {"en_US": "Translate text", "zh_Hans": "翻译文本"},
            "skill": "保持术语一致。",
        }

        resolved = resolve_skill_content(raw_skill)

        self.assertIn("翻译", resolved)
        self.assertIn("翻译文本", resolved)
        self.assertIn("保持术语一致。", resolved)
        self.assertNotIn("\"zh_Hans\"", resolved)

    def test_build_prompt_requires_skill_trace_in_output(self):
        prompt = build_prompt(
            "Role: senior reviewer\nWorkflow: findings first\nOutput: severity table",
            "review this diff",
            False,
            0,
        )

        self.assertIn("必须把【已解析技能】中的角色", prompt)
        self.assertIn("Workflow: findings first", prompt)
        self.assertIn("review this diff", prompt)

    def test_renders_supported_template_variables(self):
        rendered = render_template_variables(
            "Use this context: {context}. Files: {file_count}.",
            "生成销售邮件",
            2,
        )

        self.assertIn("生成销售邮件", rendered)
        self.assertIn("Files: 2", rendered)

    def test_build_execution_prompt_runs_skill_instead_of_generating_prompt(self):
        prompt = build_execution_prompt(
            "Role: Amazon Listing Strategist\nOutput: Ask for missing sock details first.",
            "socks",
            False,
            0,
        )

        self.assertIn("直接执行【已解析技能】", prompt)
        self.assertIn("不得输出新的提示词", prompt)
        self.assertIn("Amazon Listing Strategist", prompt)
        self.assertIn("socks", prompt)

    def test_strip_reasoning_content_removes_think_blocks(self):
        result = strip_reasoning_content("<think>hidden reasoning</think>\n# Final\nVisible output")

        self.assertEqual("# Final\nVisible output", result)

    def test_missing_output_mode_defaults_to_execute_skill(self):
        self.assertEqual("execute_skill", determine_output_mode(None))
        self.assertEqual("execute_skill", determine_output_mode(""))

    def test_explicit_generate_prompt_mode_is_preserved(self):
        self.assertEqual("generate_prompt", determine_output_mode("generate_prompt"))

    def test_external_api_type_aliases_google_gemini(self):
        self.assertEqual("google_gemini", determine_external_api_type("gemini"))
        self.assertEqual("google_gemini", determine_external_api_type("google_gemini"))
        self.assertEqual("openai_compatible", determine_external_api_type(""))

    def test_extract_skill_package_reads_skill_and_references(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("my-skill/SKILL.md", "---\nname: listing\n---\n# Main Skill")
            archive.writestr("my-skill/references/checklist.md", "Reference checklist")
            archive.writestr("my-skill/references/rules.json", '{"rule":"Use confidence tags"}')
            archive.writestr("my-skill/references/image.png", b"not text")
            archive.writestr("../escape/SKILL.md", "bad")

        extracted = extract_skill_package(buffer.getvalue(), "listing.zip")

        self.assertIn("# Main Skill", extracted)
        self.assertIn("references/checklist.md", extracted)
        self.assertIn("Reference checklist", extracted)
        self.assertIn("Use confidence tags", extracted)
        self.assertNotIn("not text", extracted)
        self.assertNotIn("bad", extracted)

    def test_compose_skill_package_with_custom_supplement(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("SKILL.md", "# Main Skill")

        composed = compose_skill_from_sources(
            skill_packages=[{"filename": "skill.zip", "blob": buffer.getvalue()}],
            custom_template="Extra operator note",
            template_id="custom",
            builtin_templates={},
            context="袜子",
            file_count=0,
        )

        self.assertIn("# Main Skill", composed)
        self.assertIn("用户补充指令", composed)
        self.assertIn("Extra operator note", composed)

    def test_build_user_prompt_message_adds_image_and_text_files(self):
        message = build_user_prompt_message(
            "Use attachments.",
            [
                {
                    "filename": "product.png",
                    "mime_type": "image/png",
                    "blob": b"fake-image-bytes",
                },
                {
                    "filename": "sellpoints.txt",
                    "mime_type": "text/plain",
                    "blob": "Soft cotton\nAnti odor".encode(),
                },
            ],
        )

        self.assertIsInstance(message.content, list)
        self.assertEqual(message.content[0].type, "text")
        self.assertIn("Soft cotton", message.content[0].data)
        self.assertEqual(message.content[1].type, "image")
        self.assertEqual(message.content[1].filename, "product.png")

    def test_build_user_prompt_message_fetches_text_attachment_from_url(self):
        fake_response = FakeStreamResponse(b"Soft cotton\nAnti odor")

        with patch("tools.generate_prompt.generate_prompt.httpx.stream", return_value=fake_response):
            message = build_user_prompt_message(
                "Use attachments.",
                [
                    {
                        "filename": "sellpoints.txt",
                        "mime_type": "text/plain",
                        "url": "https://example.test/sellpoints.txt",
                    },
                ],
            )

        self.assertIsInstance(message.content, str)
        self.assertIn("Soft cotton", message.content)
        self.assertIn("sellpoints.txt", message.content)

    def test_build_user_prompt_message_uses_image_url_attachment(self):
        fake_response = FakeStreamResponse(b"fake-image-bytes")

        with patch("tools.generate_prompt.generate_prompt.httpx.stream", return_value=fake_response):
            message = build_user_prompt_message(
                "Use attachments.",
                [
                    {
                        "filename": "product.png",
                        "mime_type": "image/png",
                        "url": "https://example.test/product.png",
                    },
                ],
            )

        self.assertIsInstance(message.content, list)
        self.assertEqual(message.content[1].type, "image")
        self.assertEqual(message.content[1].filename, "product.png")
        self.assertEqual(message.content[1].url, "")
        self.assertTrue(message.content[1].base64_data)

    def test_build_user_prompt_message_rejects_images_over_2k(self):
        with self.assertRaisesRegex(ValueError, "exceeds the 2048px image dimension limit"):
            build_user_prompt_message(
                "Use attachments.",
                [
                    {
                        "filename": "large.png",
                        "mime_type": "image/png",
                        "blob": make_png_header(2049, 1200),
                    },
                ],
            )

    def test_build_external_messages_adds_image_url_and_text(self):
        messages = build_external_messages(
            "System",
            "Prompt",
            [
                {
                    "filename": "photo.png",
                    "mime_type": "image/png",
                    "blob": b"fake-image-bytes",
                },
            ],
        )

        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[1]["content"][1]["type"], "image_url")

    def test_build_external_messages_rejects_images_over_2k(self):
        with self.assertRaisesRegex(ValueError, "exceeds the 2048px image dimension limit"):
            build_external_messages(
                "System",
                "Prompt",
                [
                    {
                        "filename": "large.png",
                        "mime_type": "image/png",
                        "blob": make_png_header(1200, 2049),
                    },
                ],
            )

    def test_build_google_payload_adds_inline_image_and_text(self):
        payload = build_google_generate_content_payload(
            "System",
            "Prompt",
            [
                {
                    "filename": "photo.png",
                    "mime_type": "image/png",
                    "blob": b"fake-image-bytes",
                },
                {
                    "filename": "notes.txt",
                    "mime_type": "text/plain",
                    "blob": b"Material notes",
                },
            ],
        )

        self.assertEqual(payload["system_instruction"]["parts"][0]["text"], "System")
        parts = payload["contents"][0]["parts"]
        self.assertEqual(parts[0]["text"], "Prompt")
        self.assertEqual(parts[1]["inline_data"]["mime_type"], "image/png")
        self.assertTrue(parts[1]["inline_data"]["data"])
        self.assertIn("Material notes", parts[2]["text"])
        self.assertEqual(payload["generationConfig"]["maxOutputTokens"], 4096)

    def test_build_google_payload_rejects_images_over_2k(self):
        with self.assertRaisesRegex(ValueError, "exceeds the 2048px image dimension limit"):
            build_google_generate_content_payload(
                "System",
                "Prompt",
                [
                    {
                        "filename": "large.png",
                        "mime_type": "image/png",
                        "blob": make_png_header(2049, 1200),
                    },
                ],
            )

    def test_get_tool_input_attachments_accepts_base64_image_data_url(self):
        image_data = base64.b64encode(make_png_header(64, 64)).decode("ascii")

        files = get_tool_input_attachments(
            {
                "base64_images": f"data:image/png;base64,{image_data}",
            }
        )

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["filename"], "base64_image_1.png")
        self.assertEqual(files[0]["mime_type"], "image/png")
        self.assertEqual(files[0]["blob"], make_png_header(64, 64))

    def test_get_tool_input_attachments_collects_file_slots(self):
        files = get_tool_input_attachments(
            {
                "attachment_1": {"filename": "product.png", "mime_type": "image/png", "blob": b"image"},
                "attachment_2": {"filename": "brief.txt", "mime_type": "text/plain", "blob": b"notes"},
                "attachment_3": None,
            }
        )

        self.assertEqual([file["filename"] for file in files], ["product.png", "brief.txt"])

    def test_get_tool_input_files_merges_uploaded_files_and_base64_images(self):
        image_data = base64.b64encode(make_png_header(32, 32)).decode("ascii")

        files = get_tool_input_files(
            {
                "files": [{"filename": "notes.txt", "mime_type": "text/plain", "blob": b"notes"}],
                "base64_images": f"data:image/png;base64,{image_data}",
            }
        )

        self.assertEqual(len(files), 2)
        self.assertEqual(files[0]["filename"], "notes.txt")
        self.assertEqual(files[1]["filename"], "base64_image_1.png")

    def test_call_external_llm_uses_google_generate_content_endpoint(self):
        FakeHttpClient.last_post = None

        with patch("tools.generate_prompt.generate_prompt.httpx.Client", FakeHttpClient):
            result = call_external_llm(
                "test-key",
                "",
                "gemini-1.5-pro",
                "System",
                "Prompt",
                [],
                "google_gemini",
            )

        self.assertEqual(result, "Gemini result")
        self.assertIsNotNone(FakeHttpClient.last_post)
        self.assertIn("/models/gemini-1.5-pro:generateContent", FakeHttpClient.last_post["url"])
        self.assertEqual(FakeHttpClient.last_post["headers"]["x-goog-api-key"], "test-key")
        self.assertIn("contents", FakeHttpClient.last_post["json"])

    def test_external_image_url_without_blob_uses_fetched_data_url(self):
        fake_response = FakeStreamResponse(b"fake-image-bytes")

        with patch("tools.generate_prompt.generate_prompt.httpx.stream", return_value=fake_response):
            messages = build_external_messages(
                "System",
                "Prompt",
                [
                    {
                        "filename": "photo.png",
                        "mime_type": "image/png",
                        "url": "https://example.test/photo.png",
                    },
                ],
            )

        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[1]["content"][1]["type"], "image_url")
        self.assertTrue(messages[1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_image_url_fetch_rejects_files_over_10mb_before_model_call(self):
        fake_response = FakeStreamResponse(b"", {"content-length": str(10 * 1024 * 1024 + 1)})

        with patch("tools.generate_prompt.generate_prompt.httpx.stream", return_value=fake_response):
            with self.assertRaisesRegex(ValueError, "exceeds the 10485760 byte size limit"):
                build_external_messages(
                    "System",
                    "Prompt",
                    [
                        {
                            "filename": "huge.png",
                            "mime_type": "image/png",
                            "url": "https://example.test/huge.png",
                        },
                    ],
                )

    def test_dify_image_url_fetch_failure_returns_clear_error(self):
        with patch("tools.generate_prompt.generate_prompt.httpx.stream", side_effect=RuntimeError("timeout")):
            with self.assertRaisesRegex(ValueError, "could not be read from Dify file storage"):
                build_user_prompt_message(
                    "Use attachments.",
                    [
                        {
                            "dify_model_identity": "__dify__file__",
                            "filename": "photo.png",
                            "mime_type": "image/png",
                            "url": "http://api:5001/files/photo.png",
                        },
                    ],
                )

    def test_normalize_files_ignores_empty_file_placeholders(self):
        self.assertEqual(normalize_files(""), [])
        self.assertEqual(normalize_files("[]"), [])
        self.assertEqual(normalize_files("null"), [])
        self.assertEqual(normalize_files(None), [])

    def test_save_and_load_skill_registry(self):
        storage = FakeStorage()
        save_registered_skill(
            storage,
            {
                "id": "listing",
                "name": "Listing Skill",
                "description": "Amazon listing strategy",
                "content": "# Skill",
            },
        )

        registry = load_skill_registry(storage)

        self.assertIn("listing", registry["skills"])
        self.assertEqual(get_registered_skill(storage, "listing")["content"], "# Skill")
        self.assertIn(SKILL_REGISTRY_KEY, storage.values)

    def test_registered_skill_content_is_chunked_outside_registry(self):
        storage = LimitedValueStorage(max_value_bytes=50_000)
        first_content = "A" * 30_000
        second_content = "B" * 30_000

        save_registered_skill(
            storage,
            {
                "id": "first",
                "name": "First Skill",
                "description": "",
                "source_filename": "first.zip",
                "content": first_content,
            },
        )
        save_registered_skill(
            storage,
            {
                "id": "second",
                "name": "Second Skill",
                "description": "",
                "source_filename": "second.zip",
                "content": second_content,
            },
        )

        registry = load_skill_registry(storage)

        self.assertIn("first", registry["skills"])
        self.assertIn("second", registry["skills"])
        self.assertNotIn("content", registry["skills"]["first"])
        self.assertEqual(get_registered_skill(storage, "first")["content"], first_content)
        self.assertEqual(get_registered_skill(storage, "second")["content"], second_content)
        self.assertTrue(any(key.startswith(SKILL_CONTENT_CHUNK_PREFIX) for key in storage.values))

    def test_template_runtime_parameter_contains_builtin_options(self):
        parameter = build_template_runtime_parameter()

        self.assertEqual("template", parameter.name)
        self.assertGreaterEqual(len(parameter.options or []), 5)
        self.assertIn("custom", [option.value for option in parameter.options or []])

    def test_template_options_include_registered_entries(self):
        storage = FakeStorage()
        save_registered_skill(
            storage,
            {
                "id": "listing-123",
                "name": "Listing Skill",
                "description": "Amazon listing strategy",
                "content": "# Skill",
            },
        )

        options = build_template_options(storage)
        values = [option.value for option in options]

        self.assertIn("registered:listing-123", values)

    def test_list_registered_skills_returns_metadata_without_content(self):
        storage = FakeStorage()
        save_registered_skill(
            storage,
            {
                "id": "listing-123",
                "name": "Listing Skill",
                "description": "Amazon listing strategy",
                "source_filename": "listing.zip",
                "content": "# Long Skill Body",
            },
        )

        skills = list_registered_skills(storage)

        self.assertEqual(
            skills,
            [
                {
                    "id": "listing-123",
                    "name": "Listing Skill",
                    "description": "Amazon listing strategy",
                    "source_filename": "listing.zip",
                }
            ],
        )

    def test_delete_registered_skill_removes_list_entry_and_content_chunks(self):
        storage = FakeStorage()
        save_registered_skill(
            storage,
            {
                "id": "listing-123",
                "name": "Listing Skill",
                "description": "Amazon listing strategy",
                "source_filename": "listing.zip",
                "content": "A" * 70_000,
            },
        )
        chunk_keys = [key for key in storage.values if key.startswith(SKILL_CONTENT_CHUNK_PREFIX)]
        self.assertGreater(len(chunk_keys), 1)

        deleted = delete_registered_skill(storage, "listing-123")

        self.assertTrue(deleted)
        self.assertNotIn("listing-123", load_skill_registry(storage)["skills"])
        self.assertIsNone(get_registered_skill(storage, "listing-123"))
        self.assertFalse(any(key.startswith(SKILL_CONTENT_CHUNK_PREFIX) for key in storage.values))

    def test_delete_registered_skill_returns_false_for_missing_id(self):
        self.assertFalse(delete_registered_skill(FakeStorage(), "missing-id"))

    def test_registered_skill_id_loads_skill_from_storage(self):
        storage = FakeStorage()
        save_registered_skill(
            storage,
            {
                "id": "listing-123",
                "name": "Listing Skill",
                "description": "Amazon listing strategy",
                "content": "Registered listing skill body",
            },
        )

        skill = select_skill_content(
            storage=storage,
            tool_parameters={
                "registered_skill_id": "listing-123",
                "template": "translate",
                "context": "socks",
            },
            builtin_templates={"translate": {"skill": "Built in translate skill"}},
            file_count=0,
        )

        self.assertIn("Registered listing skill body", skill)
        self.assertNotIn("Built in translate skill", skill)

    def test_registered_skill_id_accepts_registered_prefix(self):
        storage = FakeStorage()
        save_registered_skill(
            storage,
            {
                "id": "listing-123",
                "name": "Listing Skill",
                "description": "Amazon listing strategy",
                "content": "Registered listing skill body",
            },
        )

        skill = select_skill_content(
            storage=storage,
            tool_parameters={
                "registered_skill_id": "registered:listing-123",
                "template": "custom",
                "context": "socks",
            },
            builtin_templates={},
            file_count=0,
        )

        self.assertIn("Registered listing skill body", skill)

    def test_missing_registered_skill_id_returns_clear_error(self):
        with self.assertRaisesRegex(ValueError, "Registered skill 'missing-id' not found"):
            select_skill_content(
                storage=FakeStorage(),
                tool_parameters={
                    "registered_skill_id": "missing-id",
                    "template": "custom",
                    "context": "socks",
                },
                builtin_templates={},
                file_count=0,
            )

    def test_manifest_enables_storage_permission(self):
        manifest = yaml.safe_load(Path("manifest.yaml").read_text(encoding="utf-8"))

        self.assertTrue(manifest["resource"]["permission"]["storage"]["enabled"])
        self.assertGreaterEqual(manifest["resource"]["permission"]["storage"]["size"], 20 * 1024 * 1024)

    def test_plugin_icon_uses_packaged_png_asset(self):
        manifest = yaml.safe_load(Path("manifest.yaml").read_text(encoding="utf-8"))
        provider_manifest = yaml.safe_load(
            Path("provider/prompt_skill_generator.yaml").read_text(encoding="utf-8")
        )

        self.assertEqual(manifest["icon"], "icon.png")
        self.assertEqual(provider_manifest["identity"]["icon"], "icon.png")
        self.assertTrue(Path("_assets/icon.png").exists())

    def test_tool_manifest_uses_runtime_parameters(self):
        tool_manifest = yaml.safe_load(
            Path("tools/generate_prompt/generate_prompt.yaml").read_text(encoding="utf-8")
        )

        self.assertFalse(tool_manifest.get("has_runtime_parameters", False))
        parameter_names = [parameter["name"] for parameter in tool_manifest["parameters"]]
        self.assertIn("template", parameter_names)
        self.assertIn("registered_skill_id", parameter_names)
        self.assertIn("base64_images", parameter_names)
        self.assertIn("external_api_type", parameter_names)
        self.assertIn("external_model", parameter_names)
        self.assertNotIn("input_files", parameter_names)
        self.assertNotIn("skill_package", parameter_names)
        self.assertNotIn("files", parameter_names)

    def test_provider_manifest_includes_attachment_tool(self):
        provider_manifest = yaml.safe_load(
            Path("provider/prompt_skill_generator.yaml").read_text(encoding="utf-8")
        )

        self.assertIn(
            "tools/generate_prompt_with_attachments/generate_prompt_with_attachments.yaml",
            provider_manifest["tools"],
        )

    def test_register_skill_manifest_exposes_management_actions(self):
        register_manifest = yaml.safe_load(
            Path("tools/register_skill_package/register_skill_package.yaml").read_text(encoding="utf-8")
        )
        parameters_by_name = {parameter["name"]: parameter for parameter in register_manifest["parameters"]}

        self.assertIn("operation", parameters_by_name)
        self.assertEqual(parameters_by_name["operation"]["type"], "select")
        self.assertEqual(parameters_by_name["operation"]["label"]["zh_Hans"], "操作类型")
        self.assertEqual(parameters_by_name["skill_package"]["type"], "file")
        self.assertFalse(parameters_by_name["skill_package"]["required"])
        self.assertNotIn("default", parameters_by_name["skill_package"])
        self.assertIn("skill_id", parameters_by_name)
        self.assertEqual(parameters_by_name["skill_id"]["label"]["zh_Hans"], "Skill ID")
        self.assertEqual(
            [option["value"] for option in parameters_by_name["operation"]["options"]],
            ["register", "list", "delete"],
        )

    def test_attachment_tool_manifest_uses_single_file_slots(self):
        attachment_manifest_path = Path("tools/generate_prompt_with_attachments/generate_prompt_with_attachments.yaml")
        self.assertTrue(attachment_manifest_path.exists())

        attachment_manifest = yaml.safe_load(attachment_manifest_path.read_text(encoding="utf-8"))
        parameter_names = [parameter["name"] for parameter in attachment_manifest["parameters"]]
        parameters_by_name = {parameter["name"]: parameter for parameter in attachment_manifest["parameters"]}

        self.assertIn("attachment_1", parameter_names)
        self.assertIn("attachment_5", parameter_names)
        self.assertNotIn("attachment_6", parameter_names)
        self.assertEqual(parameters_by_name["attachment_1"]["type"], "file")
        self.assertEqual(parameters_by_name["attachment_1"]["label"]["zh_Hans"], "附件 1")
        self.assertFalse(parameters_by_name["attachment_1"]["required"])
        for index in range(1, 6):
            self.assertNotIn("default", parameters_by_name[f"attachment_{index}"])
        self.assertIn("base64_images", parameter_names)
        self.assertIn("context", parameter_names)
        self.assertIn("external_api_type", parameter_names)
        self.assertIn("external_model", parameter_names)
        self.assertNotIn("attachments", parameter_names)
        self.assertNotIn("input_files", parameter_names)
        self.assertNotIn("files", parameter_names)


if __name__ == "__main__":
    unittest.main()
