import base64
import hashlib
import io
import json
import re
import time
import zipfile
from collections.abc import Generator
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

import httpx
from dify_plugin import Tool
from dify_plugin.entities import I18nObject, ParameterOption
from dify_plugin.entities.model.llm import LLMModelConfig
from dify_plugin.entities.model.message import (
    DocumentPromptMessageContent,
    ImagePromptMessageContent,
    SystemPromptMessage,
    TextPromptMessageContent,
    UserPromptMessage,
)
from dify_plugin.entities.tool import ToolInvokeMessage
from dify_plugin.entities.tool import ToolParameter, ToolParameterOption


BUILTIN_TEMPLATES_PATH = Path(__file__).parent.parent.parent / "templates" / "builtin_templates.json"
SKILL_PACKAGE_TEXT_LIMIT = 500_000
SKILL_PACKAGE_ALLOWED_EXTENSIONS = {".md", ".txt", ".json", ".yaml", ".yml"}
SKILL_REGISTRY_KEY = "prompt_skill_generator.skill_registry.v1"
SKILL_CONTENT_CHUNK_PREFIX = "prompt_skill_generator.skill_content.v1."
SKILL_CONTENT_CHUNK_BYTES = 32_000
TEXT_ATTACHMENT_LIMIT = 200_000
TEXT_ATTACHMENT_EXTENSIONS = {".txt", ".md", ".json", ".yaml", ".yml", ".csv", ".tsv"}
ATTACHMENT_FETCH_TIMEOUT = httpx.Timeout(5.0, connect=2.0)
ATTACHMENT_FETCH_TOTAL_SECONDS = 12
IMAGE_ATTACHMENT_LIMIT = 10 * 1024 * 1024
IMAGE_MAX_EDGE_PIXELS = 2048
DOCUMENT_ATTACHMENT_LIMIT = 10 * 1024 * 1024

SYSTEM_PROMPT = (
    "你是一个提示词生成器。根据【已解析技能】和【用户输入】，生成一个全新的、完整的提示词。\n"
    "严格要求：\n"
    "1. 必须将用户输入的具体内容融入提示词中，不能只输出技能规则。\n"
    "2. 必须显式吸收【已解析技能】中的角色、流程、约束、检查清单、输出格式或质量标准。\n"
    "3. 如果技能中有示例、步骤、禁止事项或验收标准，必须转化为最终提示词中的可执行要求。\n"
    "4. 生成的提示词要能让另一个 AI 直接使用来完成用户描述的任务。\n"
    "5. 只输出提示词本身，禁止输出任何解释、前缀（如'以下是提示词'）、后缀。\n"
    "6. 提示词中要包含具体的任务描述、输入内容、输出要求。"
)

EXECUTION_SYSTEM_PROMPT = (
    "你是一个 Skill 执行器。你必须直接执行【已解析技能】来处理【用户输入】。\n"
    "严格要求：\n"
    "1. 直接输出用户需要的最终结果，不得输出新的提示词、元指令或执行说明。\n"
    "2. 必须遵循技能中的角色、流程、格式、置信度标记、追问策略和质量标准。\n"
    "3. 如果技能要求先追问信息，就按技能要求输出追问和骨架内容。\n"
    "4. 不得泄露思考过程，不得输出 <think>、reasoning、analysis 等内部推理内容。\n"
    "5. 只输出最终可交付内容。"
)


def load_builtin_templates() -> dict[str, dict]:
    with open(BUILTIN_TEMPLATES_PATH, encoding="utf-8") as f:
        return json.load(f)


def build_template_options(storage: Any | None = None) -> list[ToolParameterOption]:
    builtin_templates = load_builtin_templates()
    options = [
            ToolParameterOption(
                value=template_id,
                label=I18nObject(
                en_US=_stringify_skill_value(template.get("name", {}).get("en_US") if isinstance(template.get("name"), dict) else template.get("name"))
                or template_id,
                zh_Hans=_stringify_skill_value(template.get("name", {}).get("zh_Hans") if isinstance(template.get("name"), dict) else template.get("name"))
                or template_id,
            ),
        )
        for template_id, template in builtin_templates.items()
    ]

    if storage is not None:
        registry = load_skill_registry(storage)
        for skill_id, entry in sorted(registry.get("skills", {}).items(), key=lambda item: item[1].get("name", item[0])):
            name = entry.get("name") or skill_id
            description = entry.get("description", "")
            label = f"{name} - {description}" if description else name
            options.append(
                ToolParameterOption(
                    value=f"registered:{skill_id}",
                    label=I18nObject(en_US=label, zh_Hans=label),
                )
            )

    options.append(ToolParameterOption(value="custom", label=I18nObject(en_US="Custom", zh_Hans="自定义")))
    return options


def load_skill_registry(storage: Any) -> dict[str, dict]:
    try:
        raw = storage.get(SKILL_REGISTRY_KEY)
    except Exception:
        return {"skills": {}}

    try:
        registry = json.loads(raw.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
        return {"skills": {}}

    if not isinstance(registry, dict) or not isinstance(registry.get("skills"), dict):
        return {"skills": {}}
    return registry


def save_skill_registry(storage: Any, registry: dict[str, dict]) -> None:
    storage.set(SKILL_REGISTRY_KEY, json.dumps(registry, ensure_ascii=False).encode("utf-8"))


def _skill_content_chunk_key(skill_id: str, index: int) -> str:
    return f"{SKILL_CONTENT_CHUNK_PREFIX}{skill_id}.{index}"


def _write_skill_content_chunks(storage: Any, skill_id: str, content: str) -> int:
    data = content.encode("utf-8")
    chunks = max(1, (len(data) + SKILL_CONTENT_CHUNK_BYTES - 1) // SKILL_CONTENT_CHUNK_BYTES)
    for index in range(chunks):
        start = index * SKILL_CONTENT_CHUNK_BYTES
        storage.set(_skill_content_chunk_key(skill_id, index), data[start : start + SKILL_CONTENT_CHUNK_BYTES])
    return chunks


def _read_skill_content_chunks(storage: Any, skill_id: str, chunk_count: int) -> str:
    chunks = []
    for index in range(chunk_count):
        chunks.append(storage.get(_skill_content_chunk_key(skill_id, index)))
    return b"".join(chunks).decode("utf-8")


def _registry_entry_without_inline_content(storage: Any, entry: dict[str, Any]) -> dict[str, Any]:
    entry = dict(entry)
    content = str(entry.pop("content", "") or "")
    skill_id = str(entry.get("id") or "")
    if content and skill_id:
        entry["content_storage"] = "chunks_v1"
        entry["content_chunk_count"] = _write_skill_content_chunks(storage, skill_id, content)
    return entry


def save_registered_skill(storage: Any, entry: dict[str, str]) -> dict[str, dict]:
    registry = load_skill_registry(storage)
    skills = registry.setdefault("skills", {})
    for skill_id, existing_entry in list(skills.items()):
        if isinstance(existing_entry, dict) and "content" in existing_entry:
            migrated_entry = dict(existing_entry)
            migrated_entry.setdefault("id", skill_id)
            skills[skill_id] = _registry_entry_without_inline_content(storage, migrated_entry)

    skills[entry["id"]] = _registry_entry_without_inline_content(storage, entry)
    save_skill_registry(storage, registry)
    return registry


def skill_entry_id(name: str, content: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip().lower()).strip("-") or "skill"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


def extract_skill_metadata(skill_text: str, fallback_name: str) -> tuple[str, str]:
    name_match = re.search(r"(?im)^\s*name\s*:\s*[\"']?(.+?)[\"']?\s*$", skill_text[:4000])
    description_match = re.search(r"(?im)^\s*description\s*:\s*[\"']?(.+?)[\"']?\s*$", skill_text[:4000])
    name = name_match.group(1).strip() if name_match else PurePosixPath(fallback_name).stem
    description = description_match.group(1).strip() if description_match else ""
    return name, description


def make_registered_skill_entry(package: Any, name_override: str = "", description_override: str = "") -> dict[str, str]:
    filename = _uploaded_filename(package)
    content = extract_skill_package(_read_uploaded_blob(package), filename)
    inferred_name, inferred_description = extract_skill_metadata(content, filename)
    name = name_override.strip() or inferred_name
    description = description_override.strip() or inferred_description
    return {
        "id": skill_entry_id(name, content),
        "name": name,
        "description": description,
        "source_filename": filename,
        "content": resolve_skill_content(content),
    }


def get_registered_skill(storage: Any, skill_id: str) -> dict[str, str] | None:
    entry = load_skill_registry(storage).get("skills", {}).get(skill_id)
    if not entry:
        return None

    entry = dict(entry)
    if "content" in entry:
        return entry

    if entry.get("content_storage") == "chunks_v1":
        try:
            chunk_count = int(entry.get("content_chunk_count") or 0)
            entry["content"] = _read_skill_content_chunks(storage, skill_id, chunk_count)
        except Exception:
            entry["content"] = ""
    return entry


def list_registered_skills(storage: Any) -> list[dict[str, str]]:
    registry = load_skill_registry(storage)
    skills = []
    for skill_id, entry in sorted(registry.get("skills", {}).items(), key=lambda item: item[1].get("name", item[0])):
        skills.append(
            {
                "id": skill_id,
                "name": str(entry.get("name") or skill_id),
                "description": str(entry.get("description") or ""),
                "source_filename": str(entry.get("source_filename") or ""),
            }
        )
    return skills


def _delete_storage_key(storage: Any, key: str) -> None:
    delete = getattr(storage, "delete", None)
    if callable(delete):
        delete(key)


def delete_registered_skill(storage: Any, skill_id: str) -> bool:
    registry = load_skill_registry(storage)
    skills = registry.get("skills", {})
    entry = skills.pop(skill_id, None)
    if not entry:
        return False

    if isinstance(entry, dict) and entry.get("content_storage") == "chunks_v1":
        try:
            chunk_count = int(entry.get("content_chunk_count") or 0)
        except (TypeError, ValueError):
            chunk_count = 0
        for index in range(chunk_count):
            _delete_storage_key(storage, _skill_content_chunk_key(skill_id, index))

    save_skill_registry(storage, registry)
    return True


def _is_safe_zip_path(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute():
        return False
    return ".." not in path.parts


def _decode_text(data: bytes, name: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Skill package text file '{name}' is not UTF-8 encoded.") from exc


def _read_uploaded_blob(file_item: Any) -> bytes:
    if isinstance(file_item, bytes):
        return file_item
    if isinstance(file_item, dict):
        blob = file_item.get("blob") or file_item.get("_blob") or file_item.get("content")
        if isinstance(blob, bytes):
            return blob
    blob = getattr(file_item, "blob", None)
    if isinstance(blob, bytes):
        return blob
    raise ValueError("Skill package file content is unavailable.")


def _fetch_uploaded_url_blob(file_item: Any) -> bytes | None:
    url = _uploaded_url(file_item)
    if not url.startswith(("http://", "https://")):
        return None

    size_limit = DOCUMENT_ATTACHMENT_LIMIT
    if _is_image_file(file_item):
        size_limit = IMAGE_ATTACHMENT_LIMIT

    started_at = time.monotonic()
    chunks: list[bytes] = []
    total_size = 0

    with httpx.stream("GET", url, timeout=ATTACHMENT_FETCH_TIMEOUT, follow_redirects=True) as response:
        response.raise_for_status()
        content_length = response.headers.get("content-length")
        if content_length and int(content_length) > size_limit:
            filename = _uploaded_filename(file_item)
            raise ValueError(f"Attachment '{filename}' exceeds the {size_limit} byte size limit.")

        for chunk in response.iter_bytes(chunk_size=64 * 1024):
            if time.monotonic() - started_at > ATTACHMENT_FETCH_TOTAL_SECONDS:
                filename = _uploaded_filename(file_item)
                raise TimeoutError(f"Timed out reading attachment '{filename}'.")
            if not chunk:
                continue
            total_size += len(chunk)
            if total_size > size_limit:
                filename = _uploaded_filename(file_item)
                raise ValueError(f"Attachment '{filename}' exceeds the {size_limit} byte size limit.")
            chunks.append(chunk)

    return b"".join(chunks)


def _optional_uploaded_blob(file_item: Any) -> bytes | None:
    try:
        return _read_uploaded_blob(file_item)
    except Exception:
        try:
            return _fetch_uploaded_url_blob(file_item)
        except ValueError:
            raise
        except Exception as exc:
            if _is_dify_file_parameter(file_item):
                filename = _uploaded_filename(file_item)
                raise ValueError(
                    f"Attachment '{filename}' could not be read from Dify file storage: {exc!s}"
                ) from exc
            return None


def _read_uint24_le(data: bytes, offset: int) -> int:
    return data[offset] | (data[offset + 1] << 8) | (data[offset + 2] << 16)


def _image_dimensions(data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")

    if data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")

    if data.startswith(b"RIFF") and len(data) >= 30 and data[8:12] == b"WEBP":
        chunk_type = data[12:16]
        if chunk_type == b"VP8X" and len(data) >= 30:
            return _read_uint24_le(data, 24) + 1, _read_uint24_le(data, 27) + 1
        if chunk_type == b"VP8 " and len(data) >= 30:
            return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
        if chunk_type == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1

    if data.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 9 < len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            offset += 2
            while marker == 0xFF and offset < len(data):
                marker = data[offset]
                offset += 1
            if marker in {0xD8, 0xD9}:
                continue
            if offset + 2 > len(data):
                return None
            segment_length = int.from_bytes(data[offset : offset + 2], "big")
            if segment_length < 2 or offset + segment_length > len(data):
                return None
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                height = int.from_bytes(data[offset + 3 : offset + 5], "big")
                width = int.from_bytes(data[offset + 5 : offset + 7], "big")
                return width, height
            offset += segment_length

    return None


def validate_image_attachment(file_item: Any, blob: bytes | None) -> None:
    if not blob:
        return

    filename = _uploaded_filename(file_item)
    if len(blob) > IMAGE_ATTACHMENT_LIMIT:
        raise ValueError(f"Attachment image '{filename}' exceeds the {IMAGE_ATTACHMENT_LIMIT} byte size limit.")

    dimensions = _image_dimensions(blob)
    if not dimensions:
        return

    width, height = dimensions
    if width > IMAGE_MAX_EDGE_PIXELS or height > IMAGE_MAX_EDGE_PIXELS:
        raise ValueError(
            f"Attachment image '{filename}' is {width}x{height}px and exceeds the "
            f"{IMAGE_MAX_EDGE_PIXELS}px image dimension limit."
        )


def _uploaded_filename(file_item: Any, fallback: str = "skill_package.zip") -> str:
    if isinstance(file_item, dict):
        return str(file_item.get("filename") or file_item.get("name") or fallback)
    return str(getattr(file_item, "filename", None) or getattr(file_item, "name", None) or fallback)


def _uploaded_mime_type(file_item: Any) -> str:
    if isinstance(file_item, dict):
        return str(file_item.get("mime_type") or file_item.get("type") or "")
    return str(getattr(file_item, "mime_type", None) or getattr(file_item, "type", None) or "")


def _uploaded_url(file_item: Any) -> str:
    if isinstance(file_item, dict):
        return str(file_item.get("url") or "")
    return str(getattr(file_item, "url", None) or "")


def _is_dify_file_parameter(file_item: Any) -> bool:
    if isinstance(file_item, dict):
        return bool(file_item.get("dify_model_identity"))
    return bool(getattr(file_item, "dify_model_identity", None))


def _uploaded_extension(file_item: Any) -> str:
    if isinstance(file_item, dict):
        extension = file_item.get("extension")
    else:
        extension = getattr(file_item, "extension", None)
    if extension:
        extension = str(extension)
        return extension if extension.startswith(".") else f".{extension}"
    return PurePosixPath(_uploaded_filename(file_item)).suffix


def _is_image_file(file_item: Any) -> bool:
    mime_type = _uploaded_mime_type(file_item).lower()
    return mime_type.startswith("image/") or _uploaded_extension(file_item).lower() in {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
    }


def _is_text_file(file_item: Any) -> bool:
    mime_type = _uploaded_mime_type(file_item).lower()
    return (
        mime_type.startswith("text/")
        or "json" in mime_type
        or "yaml" in mime_type
        or _uploaded_extension(file_item).lower() in TEXT_ATTACHMENT_EXTENSIONS
    )


def _is_document_file(file_item: Any) -> bool:
    return not _is_image_file(file_item) and not _is_text_file(file_item)


def extract_skill_package(package_blob: bytes, filename: str = "skill_package.zip") -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(package_blob))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Skill package '{filename}' is not a valid ZIP file.") from exc

    safe_infos = [
        info
        for info in archive.infolist()
        if not info.is_dir() and _is_safe_zip_path(info.filename)
    ]

    skill_infos = sorted(
        [info for info in safe_infos if PurePosixPath(info.filename).name.lower() == "skill.md"],
        key=lambda info: (len(PurePosixPath(info.filename).parts), info.filename.lower()),
    )
    if not skill_infos:
        raise ValueError(f"Skill package '{filename}' must contain a SKILL.md file.")

    skill_info = skill_infos[0]
    total_size = skill_info.file_size
    if total_size > SKILL_PACKAGE_TEXT_LIMIT:
        raise ValueError(f"Skill package '{filename}' exceeds the text size limit.")

    sections = [
        f"【Skill Package: {filename} / {skill_info.filename}】\n"
        f"{_decode_text(archive.read(skill_info), skill_info.filename).strip()}"
    ]

    reference_infos = sorted(
        [
            info
            for info in safe_infos
            if "references" in PurePosixPath(info.filename).parts
            and PurePosixPath(info.filename).suffix.lower() in SKILL_PACKAGE_ALLOWED_EXTENSIONS
            and info.filename != skill_info.filename
        ],
        key=lambda info: info.filename.lower(),
    )

    for info in reference_infos:
        total_size += info.file_size
        if total_size > SKILL_PACKAGE_TEXT_LIMIT:
            sections.append(
                f"【Reference Skipped: {info.filename}】\n"
                "Skipped because the skill package text size limit was reached."
            )
            break
        sections.append(
            f"【Reference: {info.filename}】\n"
            f"{_decode_text(archive.read(info), info.filename).strip()}"
        )

    return "\n\n".join(section for section in sections if section.strip()).strip()


def extract_uploaded_skill_packages(skill_packages: list[Any]) -> str:
    sections = []
    for index, package in enumerate(skill_packages, start=1):
        blob = _read_uploaded_blob(package)
        filename = _uploaded_filename(package, f"skill_package_{index}.zip")
        sections.append(extract_skill_package(blob, filename))
    return "\n\n".join(sections).strip()


def _parse_simple_frontmatter(frontmatter: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    current_key = ""

    for raw_line in frontmatter.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        if line[:1].isspace() and current_key:
            metadata[current_key] = f"{metadata[current_key]}\n{line.strip()}".strip()
            continue

        if ":" not in line:
            continue

        key, value = line.split(":", 1)
        current_key = key.strip()
        metadata[current_key] = value.strip().strip("\"'")

    return metadata


def _frontmatter_parts(text: str) -> tuple[dict[str, str], str]:
    stripped = text.strip()
    if not stripped.startswith("---"):
        return {}, stripped

    lines = stripped.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, stripped

    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            metadata = _parse_simple_frontmatter("\n".join(lines[1:index]))
            body = "\n".join(lines[index + 1 :]).strip()
            return metadata, body

    return {}, stripped


def _stringify_skill_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(_stringify_skill_value(item) for item in value if _stringify_skill_value(item)).strip()
    if isinstance(value, dict):
        for locale_key in ("zh_Hans", "zh_CN", "en_US", "en"):
            localized = _stringify_skill_value(value.get(locale_key))
            if localized:
                return localized

        preferred_keys = (
            "skill",
            "instructions",
            "instruction",
            "prompt",
            "content",
            "body",
            "rules",
        )
        parts = []
        for key in preferred_keys:
            text = _stringify_skill_value(value.get(key))
            if text:
                parts.append(text)
        if parts:
            return "\n".join(parts).strip()
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value).strip()


def resolve_skill_content(raw_skill: Any) -> str:
    """Normalize common skill formats into a compact instruction document."""
    if isinstance(raw_skill, dict):
        metadata = {
            "name": _stringify_skill_value(raw_skill.get("name") or raw_skill.get("id")),
            "description": _stringify_skill_value(raw_skill.get("description")),
        }
        body = _stringify_skill_value(raw_skill)
    else:
        text = _stringify_skill_value(raw_skill)
        metadata, body = _frontmatter_parts(text)

        if not metadata:
            try:
                parsed = json.loads(text)
            except (TypeError, json.JSONDecodeError):
                parsed = None

            if isinstance(parsed, dict):
                return resolve_skill_content(parsed)

    sections = []
    name = _stringify_skill_value(metadata.get("name") or metadata.get("id"))
    description = _stringify_skill_value(metadata.get("description"))

    if name:
        sections.append(f"技能名称：{name}")
    if description:
        sections.append(f"技能描述：{description}")
    if body:
        sections.append(f"技能正文：\n{body}")

    return "\n\n".join(sections).strip()


def render_template_variables(template: str, context: str, file_count: int) -> str:
    replacements = {
        "{context}": context,
        "{user_input}": context,
        "{input}": context,
        "{file_count}": str(file_count),
        "{files}": f"{file_count} 个文件" if file_count else "无文件",
    }

    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def compose_skill_from_sources(
    skill_packages: list[Any],
    custom_template: Any,
    template_id: str,
    builtin_templates: dict[str, dict],
    context: str,
    file_count: int,
) -> str:
    if skill_packages:
        skill_parts = [extract_uploaded_skill_packages(skill_packages)]
        if custom_template:
            if isinstance(custom_template, str):
                supplemental = render_template_variables(custom_template, context, file_count)
            else:
                supplemental = _stringify_skill_value(custom_template)
            skill_parts.append(f"【用户补充指令】\n{supplemental}")
        return resolve_skill_content("\n\n".join(part for part in skill_parts if part.strip()))

    if custom_template:
        if isinstance(custom_template, str):
            rendered_template = render_template_variables(custom_template, context, file_count)
        else:
            rendered_template = custom_template
        return resolve_skill_content(rendered_template)

    if template_id == "custom":
        raise ValueError("Custom Skill Content or Skill Package is required when using the custom template.")

    template = builtin_templates.get(template_id)
    if not template:
        raise ValueError(f"Template '{template_id}' not found.")
    return resolve_skill_content(template)


def normalize_registered_skill_id(value: Any) -> str:
    skill_id = str(value or "").strip()
    if skill_id.startswith("registered:"):
        skill_id = skill_id.split(":", 1)[1].strip()
    return skill_id


def select_skill_content(
    storage: Any,
    tool_parameters: dict[str, Any],
    builtin_templates: dict[str, dict],
    file_count: int,
) -> str:
    context = tool_parameters.get("context", "")
    custom_template = tool_parameters.get("custom_template", "")
    skill_packages = normalize_files(tool_parameters.get("skill_package", []))
    template_id = str(tool_parameters.get("template") or "custom")
    registered_skill_id = normalize_registered_skill_id(tool_parameters.get("registered_skill_id", ""))

    if registered_skill_id and not skill_packages:
        registered_skill = get_registered_skill(storage, registered_skill_id)
        if not registered_skill:
            raise ValueError(f"Registered skill '{registered_skill_id}' not found.")

        skill = registered_skill.get("content", "")
        if custom_template:
            supplemental = (
                render_template_variables(custom_template, context, file_count)
                if isinstance(custom_template, str)
                else _stringify_skill_value(custom_template)
            )
            skill = resolve_skill_content(f"{skill}\n\n[User Supplemental Instructions]\n{supplemental}")
        return skill

    return compose_skill_from_sources(
        skill_packages=skill_packages,
        custom_template=custom_template,
        template_id=template_id,
        builtin_templates=builtin_templates,
        context=context,
        file_count=file_count,
    )


def build_template_runtime_parameter(storage: Any | None = None) -> ToolParameter:
    return ToolParameter(
        name="template",
        type=ToolParameter.ToolParameterType.SELECT,
        label=I18nObject(en_US="Skill Template", zh_Hans="技能模板"),
        human_description=I18nObject(
            en_US="Select a built-in skill, a registered Skill ZIP preset, or Custom.",
            zh_Hans="选择内置 Skill、已注册的 Skill ZIP 预设，或自定义。",
        ),
        required=True,
        form=ToolParameter.ToolParameterForm.FORM,
        options=build_template_options(storage),
    )


def normalize_files(files: Any) -> list[Any]:
    if isinstance(files, str) and files.strip().lower() in {"", "[]", "null", "none"}:
        return []
    if isinstance(files, list):
        return files
    return [files] if files else []


def _image_extension_from_mime(mime_type: str) -> str:
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    return mapping.get(mime_type.lower(), ".png")


def _decode_base64_image_entry(entry: Any, index: int) -> dict[str, Any]:
    mime_type = "image/png"
    filename = f"base64_image_{index}.png"

    if isinstance(entry, dict):
        raw_data = str(entry.get("base64") or entry.get("data") or entry.get("content") or "").strip()
        mime_type = str(entry.get("mime_type") or entry.get("mime") or entry.get("type") or mime_type).strip()
        filename = str(entry.get("filename") or entry.get("name") or filename).strip()
    else:
        raw_data = str(entry or "").strip()

    if raw_data.startswith("data:"):
        header, separator, payload = raw_data.partition(",")
        if not separator:
            raise ValueError(f"Base64 image {index} is not a valid data URL.")
        mime_match = re.match(r"data:([^;]+);base64", header, flags=re.IGNORECASE)
        if mime_match:
            mime_type = mime_match.group(1).strip()
        raw_data = payload.strip()

    compact_data = re.sub(r"\s+", "", raw_data)
    if not compact_data:
        raise ValueError(f"Base64 image {index} is empty.")

    try:
        blob = base64.b64decode(compact_data, validate=True)
    except Exception as exc:
        raise ValueError(f"Base64 image {index} is not valid base64 data.") from exc

    if not filename or filename == f"base64_image_{index}.png":
        filename = f"base64_image_{index}{_image_extension_from_mime(mime_type)}"

    file_item = {
        "filename": filename,
        "mime_type": mime_type,
        "blob": blob,
    }
    validate_image_attachment(file_item, blob)
    return file_item


def parse_base64_image_inputs(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []

    entries: list[Any]
    if isinstance(value, list):
        entries = value
    elif isinstance(value, dict):
        entries = [value]
    else:
        text = str(value).strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            entries = parsed
        elif isinstance(parsed, dict):
            entries = [parsed]
        else:
            entries = [line.strip() for line in text.splitlines() if line.strip()]

    return [_decode_base64_image_entry(entry, index) for index, entry in enumerate(entries, start=1)]


def get_base64_image_files(tool_parameters: dict[str, Any]) -> list[Any]:
    images: list[Any] = []
    for key in ("base64_images", "image_base64", "base64_image"):
        images.extend(parse_base64_image_inputs(tool_parameters.get(key)))
    return images


def describe_files(files: list[Any]) -> str:
    descriptions = []
    for index, file_item in enumerate(files, start=1):
        if isinstance(file_item, dict):
            name = file_item.get("name") or file_item.get("filename") or file_item.get("file_name")
            file_type = file_item.get("mime_type") or file_item.get("type") or file_item.get("extension")
            source = file_item.get("url") or file_item.get("remote_url") or file_item.get("path")
        else:
            name = getattr(file_item, "name", None) or getattr(file_item, "filename", None)
            file_type = getattr(file_item, "mime_type", None) or getattr(file_item, "type", None)
            source = getattr(file_item, "url", None) or getattr(file_item, "remote_url", None) or getattr(file_item, "path", None)

        detail_parts = [part for part in (name, file_type, source) if part]
        descriptions.append(f"{index}. {' | '.join(str(part) for part in detail_parts) if detail_parts else '未命名文件'}")

    return "\n".join(descriptions)


def build_user_prompt_message(user_prompt: str, files: list[Any]) -> UserPromptMessage:
    text_prompt = user_prompt
    multimodal_parts = []

    for index, file_item in enumerate(files, start=1):
        filename = _uploaded_filename(file_item, f"file_{index}")
        mime_type = _uploaded_mime_type(file_item) or "application/octet-stream"
        url = _uploaded_url(file_item)
        blob = _optional_uploaded_blob(file_item)

        if _is_text_file(file_item) and blob:
            try:
                text_content = _decode_text(blob[:TEXT_ATTACHMENT_LIMIT], filename)
            except ValueError:
                text_content = ""
            if text_content:
                text_prompt += f"\n\n【附件文本内容：{filename}】\n{text_content.strip()}"
            continue

        if _is_image_file(file_item):
            if not blob and _is_dify_file_parameter(file_item):
                raise ValueError(
                    f"Attachment image '{filename}' could not be read from Dify file storage. "
                    "Check that FILES_URL or INTERNAL_FILES_URL is reachable from the plugin runtime."
                )
            if not blob and not url:
                raise ValueError(f"Attachment image '{filename}' has no readable content or URL.")
            validate_image_attachment(file_item, blob)
            multimodal_parts.append(
                ImagePromptMessageContent(
                    format=_uploaded_extension(file_item).lstrip(".") or "image",
                    url="" if blob else url,
                    base64_data=base64.b64encode(blob).decode("ascii") if blob else "",
                    mime_type=mime_type,
                    filename=filename,
                )
            )
            continue

        if _is_document_file(file_item) and blob:
            multimodal_parts.append(
                DocumentPromptMessageContent(
                    format=_uploaded_extension(file_item).lstrip(".") or "document",
                    url="",
                    base64_data=base64.b64encode(blob).decode("ascii"),
                    mime_type=mime_type,
                    filename=filename,
                )
            )
            continue

        if _is_document_file(file_item):
            if _is_dify_file_parameter(file_item):
                raise ValueError(
                    f"Attachment document '{filename}' could not be read from Dify file storage. "
                    "Check that FILES_URL or INTERNAL_FILES_URL is reachable from the plugin runtime."
                )
            text_prompt += f"\n\n[Attachment Document Reference] {filename}"
            if url:
                text_prompt += f" | {url}"
            continue

    if not multimodal_parts:
        return UserPromptMessage(content=text_prompt)

    return UserPromptMessage(
        content=[
            TextPromptMessageContent(data=text_prompt),
            *multimodal_parts,
        ]
    )


def get_tool_input_files(tool_parameters: dict[str, Any]) -> list[Any]:
    return [
        *normalize_files(tool_parameters.get("input_files", tool_parameters.get("files", []))),
        *get_base64_image_files(tool_parameters),
    ]


def get_attachment_slot_files(tool_parameters: dict[str, Any]) -> list[Any]:
    files: list[Any] = []
    for index in range(1, 11):
        files.extend(normalize_files(tool_parameters.get(f"attachment_{index}")))
    return files


def get_tool_input_attachments(tool_parameters: dict[str, Any]) -> list[Any]:
    return [
        *get_attachment_slot_files(tool_parameters),
        *normalize_files(
            tool_parameters.get("attachments", tool_parameters.get("input_files", tool_parameters.get("files", [])))
        ),
        *get_base64_image_files(tool_parameters),
    ]


def build_external_messages(system_prompt: str, user_prompt: str, files: list[Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]

    for index, file_item in enumerate(files, start=1):
        filename = _uploaded_filename(file_item, f"file_{index}")
        mime_type = _uploaded_mime_type(file_item) or "application/octet-stream"
        url = _uploaded_url(file_item)
        blob = _optional_uploaded_blob(file_item)

        if _is_text_file(file_item) and blob:
            try:
                text_content = _decode_text(blob[:TEXT_ATTACHMENT_LIMIT], filename).strip()
            except ValueError:
                text_content = ""
            if text_content:
                content.append({"type": "text", "text": f"[Attachment Text] {filename}\n{text_content}"})
            continue

        if _is_image_file(file_item):
            if not blob and _is_dify_file_parameter(file_item):
                raise ValueError(
                    f"Attachment image '{filename}' could not be read from Dify file storage. "
                    "Check that FILES_URL or INTERNAL_FILES_URL is reachable from the plugin runtime."
                )
            validate_image_attachment(file_item, blob)
            image_url = f"data:{mime_type};base64,{base64.b64encode(blob).decode('ascii')}" if blob else url
            if image_url:
                content.append({"type": "image_url", "image_url": {"url": image_url}})
            else:
                raise ValueError(f"Attachment image '{filename}' has no readable content or URL.")
            continue

        if _is_document_file(file_item) and blob:
            try:
                text_content = _decode_text(blob[:TEXT_ATTACHMENT_LIMIT], filename).strip()
            except ValueError:
                text_content = ""
            if text_content:
                content.append({"type": "text", "text": f"[Attachment Document] {filename}\n{text_content}"})
            else:
                content.append({"type": "text", "text": f"[Attachment Document Reference] {filename}"})
            continue

        if _is_document_file(file_item) and _is_dify_file_parameter(file_item):
            raise ValueError(
                f"Attachment document '{filename}' could not be read from Dify file storage. "
                "Check that FILES_URL or INTERNAL_FILES_URL is reachable from the plugin runtime."
            )

        content.append({"type": "text", "text": f"[Attachment File] {filename} | {url or mime_type}"})

    messages.append({"role": "user", "content": content if len(content) > 1 else user_prompt})
    return messages

def build_prompt(skill: str, context: str, has_files: bool, file_count: int, file_summary: str = "") -> str:
    parts = [
        f"【已解析技能】\n{skill}",
        f"\n【用户输入】\n{context}",
    ]

    if has_files:
        attachment = f"\n【附件】用户提供了 {file_count} 个文件，请在生成的提示词中引用这些文件。"
        if file_summary:
            attachment = f"{attachment}\n{file_summary}"
        parts.append(attachment)

    parts.append(
        "\n【生成要求】\n"
        "1. 必须把【已解析技能】中的角色、工作流、约束、输出格式和质量标准转写进最终提示词。\n"
        "2. 不要只概括技能名称；要让最终提示词看得出使用了技能正文里的具体规则。\n"
        "3. 如果技能正文包含 Markdown 标题、清单、示例、禁止事项或验收标准，请保留其任务价值并改写为可执行指令。\n"
        "4. 必须融入【用户输入】的具体任务和内容。\n"
        "5. 只输出最终提示词，不输出解释。\n\n"
        "现在请根据上面的【已解析技能】和【用户输入】，生成一个全新的提示词。直接输出："
    )

    return "\n".join(parts)


def build_execution_prompt(skill: str, context: str, has_files: bool, file_count: int, file_summary: str = "") -> str:
    parts = [
        f"【已解析技能】\n{skill}",
        f"\n【用户输入】\n{context}",
    ]

    if has_files:
        attachment = f"\n【附件】用户提供了 {file_count} 个文件，请在执行技能时引用这些文件。"
        if file_summary:
            attachment = f"{attachment}\n{file_summary}"
        parts.append(attachment)

    parts.append(
        "\n【执行要求】\n"
        "1. 直接执行【已解析技能】，产出用户要的最终内容。\n"
        "2. 不得输出新的提示词，不得把技能改写成给另一个 AI 的指令。\n"
        "3. 必须使用【用户输入】作为任务输入。\n"
        "4. 如果【用户输入】或【附件】中已经包含图片、文件、素材、路径或 URL，必须视为已提供资料，不得重复追问这些已提供项。\n"
        "5. 如果信息不足，按技能要求先诊断缺失并追问，同时输出允许的骨架内容。\n"
        "6. 不得输出思考过程、推理过程或 <think> 标签。\n\n"
        "现在开始直接执行："
    )

    return "\n".join(parts)


def strip_reasoning_content(text: str) -> str:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<reasoning>.*?</reasoning>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()


def determine_output_mode(value: Any) -> str:
    if not value:
        return "execute_skill"

    normalized = str(value).strip().lower()
    aliases = {
        "prompt": "generate_prompt",
        "generate": "generate_prompt",
        "generate_prompt": "generate_prompt",
        "execute": "execute_skill",
        "run": "execute_skill",
        "execute_skill": "execute_skill",
    }
    return aliases.get(normalized, "execute_skill")


def determine_external_api_type(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    aliases = {
        "": "openai_compatible",
        "openai": "openai_compatible",
        "openai_compatible": "openai_compatible",
        "chat_completions": "openai_compatible",
        "google": "google_gemini",
        "gemini": "google_gemini",
        "google_gemini": "google_gemini",
    }
    return aliases.get(normalized, "openai_compatible")


def build_google_generate_content_payload(system: str, user_prompt: str, files: list[Any]) -> dict[str, Any]:
    parts: list[dict[str, Any]] = [{"text": user_prompt}]

    for index, file_item in enumerate(files, start=1):
        filename = _uploaded_filename(file_item, f"file_{index}")
        mime_type = _uploaded_mime_type(file_item) or "application/octet-stream"
        url = _uploaded_url(file_item)
        blob = _optional_uploaded_blob(file_item)

        if _is_text_file(file_item) and blob:
            try:
                text_content = _decode_text(blob[:TEXT_ATTACHMENT_LIMIT], filename).strip()
            except ValueError:
                text_content = ""
            if text_content:
                parts.append({"text": f"[Attachment Text] {filename}\n{text_content}"})
            continue

        if _is_image_file(file_item):
            if not blob and _is_dify_file_parameter(file_item):
                raise ValueError(
                    f"Attachment image '{filename}' could not be read from Dify file storage. "
                    "Check that FILES_URL or INTERNAL_FILES_URL is reachable from the plugin runtime."
                )
            if not blob:
                raise ValueError(
                    f"Attachment image '{filename}' must be readable as file content for Google Gemini API calls."
                )
            validate_image_attachment(file_item, blob)
            parts.append(
                {
                    "inline_data": {
                        "mime_type": mime_type,
                        "data": base64.b64encode(blob).decode("ascii"),
                    }
                }
            )
            continue

        if _is_document_file(file_item) and blob:
            try:
                text_content = _decode_text(blob[:TEXT_ATTACHMENT_LIMIT], filename).strip()
            except ValueError:
                text_content = ""
            if text_content:
                parts.append({"text": f"[Attachment Document] {filename}\n{text_content}"})
            else:
                parts.append({"text": f"[Attachment Document Reference] {filename}"})
            continue

        if _is_document_file(file_item) and _is_dify_file_parameter(file_item):
            raise ValueError(
                f"Attachment document '{filename}' could not be read from Dify file storage. "
                "Check that FILES_URL or INTERNAL_FILES_URL is reachable from the plugin runtime."
            )

        parts.append({"text": f"[Attachment File] {filename} | {url or mime_type}"})

    return {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 4096,
        },
    }


def _extract_google_text(data: dict[str, Any]) -> str:
    texts: list[str] = []
    for candidate in data.get("candidates", []):
        content = candidate.get("content") if isinstance(candidate, dict) else None
        if not isinstance(content, dict):
            continue
        for part in content.get("parts", []):
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
    if texts:
        return "\n".join(texts).strip()
    raise ValueError("Google Gemini response did not contain text output.")


def call_openai_compatible_llm(api_key: str, api_base: str, model: str, system: str, user_prompt: str, files: list[Any]) -> str:
    base_url = api_base.rstrip("/") if api_base else "https://api.openai.com/v1"
    url = f"{base_url}/chat/completions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "messages": build_external_messages(system, user_prompt, files),
        "temperature": 0.7,
        "max_tokens": 4096,
    }

    with httpx.Client(timeout=120) as client:
        response = client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]


def call_google_gemini_llm(api_key: str, api_base: str, model: str, system: str, user_prompt: str, files: list[Any]) -> str:
    base_url = api_base.rstrip("/") if api_base else "https://generativelanguage.googleapis.com/v1beta"
    model_name = model or "gemini-1.5-pro"
    url = f"{base_url}/models/{model_name}:generateContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }
    payload = build_google_generate_content_payload(system, user_prompt, files)

    with httpx.Client(timeout=120) as client:
        response = client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return _extract_google_text(response.json())


def call_external_llm(
    api_key: str,
    api_base: str,
    model: str,
    system: str,
    user_prompt: str,
    files: list[Any],
    api_type: str = "openai_compatible",
) -> str:
    if determine_external_api_type(api_type) == "google_gemini":
        return call_google_gemini_llm(api_key, api_base, model, system, user_prompt, files)
    return call_openai_compatible_llm(api_key, api_base, model, system, user_prompt, files)


class GeneratePromptTool(Tool):
    def _get_runtime_parameters(self) -> list[ToolParameter]:
        return []

    def _fetch_parameter_options(self, parameter: str) -> list[ParameterOption]:
        if parameter != "template":
            return []
        return build_template_options(self.session.storage)

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        template_id = tool_parameters.get("template", "custom")
        registered_skill_id = normalize_registered_skill_id(tool_parameters.get("registered_skill_id", ""))
        if registered_skill_id:
            template_id = f"registered:{registered_skill_id}"
        context = tool_parameters.get("context", "")
        custom_template = tool_parameters.get("custom_template", "")
        output_mode = determine_output_mode(tool_parameters.get("output_mode"))
        reasoning_content = tool_parameters.get("reasoning_content", "hide")
        skill_packages = normalize_files(tool_parameters.get("skill_package", []))
        file_list = get_tool_input_files(tool_parameters)
        file_summary = describe_files(file_list)

        # Load skill
        try:
            builtin_templates = load_builtin_templates()
            if isinstance(template_id, str) and template_id.startswith("registered:") and not skill_packages:
                registered_skill = get_registered_skill(self.session.storage, template_id.split(":", 1)[1])
                if not registered_skill:
                    raise ValueError(f"Registered skill '{template_id.split(':', 1)[1]}' not found.")
                skill = registered_skill.get("content", "")
                if custom_template:
                    supplemental = (
                        render_template_variables(custom_template, context, len(file_list))
                        if isinstance(custom_template, str)
                        else _stringify_skill_value(custom_template)
                    )
                    skill = resolve_skill_content(f"{skill}\n\n【用户补充指令】\n{supplemental}")
            else:
                skill = compose_skill_from_sources(
                    skill_packages=skill_packages,
                    custom_template=custom_template,
                    template_id=template_id,
                    builtin_templates=builtin_templates,
                    context=context,
                    file_count=len(file_list),
                )
        except ValueError as e:
            yield self.create_text_message(f"Error: {e!s}")
            return

        # Count files
        has_files = len(file_list) > 0

        # Build user prompt
        if output_mode == "execute_skill":
            system_prompt = EXECUTION_SYSTEM_PROMPT
            user_prompt = build_execution_prompt(skill, context, has_files, len(file_list), file_summary)
        else:
            system_prompt = SYSTEM_PROMPT
            user_prompt = build_prompt(skill, context, has_files, len(file_list), file_summary)

        # Get LLM config
        model_config = tool_parameters.get("model")
        api_key = tool_parameters.get("api_key", "")
        api_base = tool_parameters.get("api_base", "")
        external_api_type = determine_external_api_type(tool_parameters.get("external_api_type"))
        external_model = str(tool_parameters.get("external_model") or "").strip()

        try:
            if api_key:
                model_name = external_model or (model_config.get("model", "gpt-4o") if isinstance(model_config, dict) else "gpt-4o")
                result = call_external_llm(
                    api_key,
                    api_base,
                    model_name,
                    system_prompt,
                    user_prompt,
                    file_list,
                    external_api_type,
                )
            else:
                if not model_config or not isinstance(model_config, dict):
                    yield self.create_text_message("Error: No model selected.")
                    return

                provider = model_config.get("provider", "")
                model = model_config.get("model", "")

                if not provider or not model:
                    yield self.create_text_message("Error: Invalid model configuration.")
                    return

                llm_config = LLMModelConfig(
                    provider=provider,
                    model=model,
                    mode="chat",
                )

                prompt_messages = [
                    SystemPromptMessage(content=system_prompt),
                    build_user_prompt_message(user_prompt, file_list),
                ]

                llm_result = self.session.model.llm.invoke(
                    model_config=llm_config,
                    prompt_messages=prompt_messages,
                    stream=False,
                )

                if isinstance(llm_result.message.content, str):
                    result = llm_result.message.content
                else:
                    result = str(llm_result.message.content)

            if reasoning_content != "show":
                result = strip_reasoning_content(result)

            yield self.create_text_message(result.strip())

        except Exception as e:
            yield self.create_text_message(f"Error calling LLM: {e!s}")
