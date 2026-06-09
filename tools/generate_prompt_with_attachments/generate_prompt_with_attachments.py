from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage, ToolParameter
from dify_plugin.entities.model.llm import LLMModelConfig
from dify_plugin.entities.model.message import SystemPromptMessage

from tools.generate_prompt.generate_prompt import (
    EXECUTION_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_execution_prompt,
    build_prompt,
    build_user_prompt_message,
    call_external_llm,
    describe_files,
    determine_external_api_type,
    determine_output_mode,
    get_tool_input_attachments,
    load_builtin_templates,
    select_skill_content,
    strip_reasoning_content,
)


class GeneratePromptWithAttachmentsTool(Tool):
    def _get_runtime_parameters(self) -> list[ToolParameter]:
        return []

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        context = tool_parameters.get("context", "")
        output_mode = determine_output_mode(tool_parameters.get("output_mode"))
        reasoning_content = tool_parameters.get("reasoning_content", "hide")
        file_list = get_tool_input_attachments(tool_parameters)
        file_summary = describe_files(file_list)

        try:
            builtin_templates = load_builtin_templates()
            skill = select_skill_content(
                storage=self.session.storage,
                tool_parameters=tool_parameters,
                builtin_templates=builtin_templates,
                file_count=len(file_list),
            )
        except ValueError as e:
            yield self.create_text_message(f"Error: {e!s}")
            return

        if not file_list:
            yield self.create_text_message("Error: Attachments or Base64 Images are required.")
            return

        if output_mode == "execute_skill":
            system_prompt = EXECUTION_SYSTEM_PROMPT
            user_prompt = build_execution_prompt(skill, context, True, len(file_list), file_summary)
        else:
            system_prompt = SYSTEM_PROMPT
            user_prompt = build_prompt(skill, context, True, len(file_list), file_summary)

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
