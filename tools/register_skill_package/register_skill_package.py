from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage, ToolParameter

from tools.generate_prompt.generate_prompt import (
    delete_registered_skill,
    list_registered_skills,
    make_registered_skill_entry,
    normalize_files,
    normalize_registered_skill_id,
    save_registered_skill,
)


def _normalize_operation(value: Any) -> str:
    operation = str(value or "register").strip().lower()
    aliases = {
        "": "register",
        "register": "register",
        "upload": "register",
        "add": "register",
        "list": "list",
        "query": "list",
        "installed": "list",
        "delete": "delete",
        "remove": "delete",
    }
    return aliases.get(operation, "register")


def _format_skill_list(skills: list[dict[str, str]]) -> str:
    if not skills:
        return "当前没有已安装 Skill。"

    lines = [f"已安装 Skill：{len(skills)} 个"]
    for index, skill in enumerate(skills, start=1):
        description = f" - {skill['description']}" if skill.get("description") else ""
        source = f" | 来源：{skill['source_filename']}" if skill.get("source_filename") else ""
        lines.append(f"{index}. {skill['name']} | ID：{skill['id']}{description}{source}")
    return "\n".join(lines)


class RegisterSkillPackageTool(Tool):
    def _get_runtime_parameters(self) -> list[ToolParameter]:
        return []

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        operation = _normalize_operation(tool_parameters.get("operation"))

        if operation == "list":
            skills = list_registered_skills(self.session.storage)
            yield self.create_text_message(_format_skill_list(skills))
            yield self.create_json_message({"skills": skills, "count": len(skills)})
            return

        if operation == "delete":
            skill_id = normalize_registered_skill_id(tool_parameters.get("skill_id", ""))
            if not skill_id:
                yield self.create_text_message("错误：删除已安装 Skill 时必须填写 Skill ID。")
                return

            deleted = delete_registered_skill(self.session.storage, skill_id)
            if not deleted:
                yield self.create_text_message(f"未找到 Skill：{skill_id}")
                yield self.create_json_message({"deleted": False, "id": skill_id})
                return

            yield self.create_text_message(f"已删除 Skill：{skill_id}")
            yield self.create_json_message({"deleted": True, "id": skill_id})
            return

        skill_packages = normalize_files(tool_parameters.get("skill_package", []))
        skill_name = str(tool_parameters.get("skill_name") or "")
        skill_description = str(tool_parameters.get("skill_description") or "")

        if not skill_packages:
            yield self.create_text_message("错误：注册 Skill 包时请上传一个 Skill ZIP 包。")
            return

        registered = []
        try:
            for package in skill_packages:
                entry = make_registered_skill_entry(
                    package=package,
                    name_override=skill_name if len(skill_packages) == 1 else "",
                    description_override=skill_description if len(skill_packages) == 1 else "",
                )
                save_registered_skill(self.session.storage, entry)
                registered.append(
                    {
                        "id": entry["id"],
                        "name": entry["name"],
                        "description": entry["description"],
                        "source_filename": entry["source_filename"],
                    }
                )
        except Exception as e:
            yield self.create_text_message(f"注册 Skill 包失败：{e!s}")
            return

        summary_lines = ["Skill 包已注册："]
        for item in registered:
            summary_lines.append(f"- {item['name']}：{item['id']}")
        summary_lines.append("执行节点可直接选择该预设，或把 id 填入 Registered Skill ID 字段。")

        yield self.create_text_message("\n".join(summary_lines))
        yield self.create_json_message(
            {
                "registered": registered,
                "message": "Skill 包已注册。请重新打开执行节点刷新预设列表，或把返回的 id 填到 Registered Skill ID 字段。",
            }
        )
