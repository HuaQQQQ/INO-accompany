import json
from typing import Any, Dict, List, Optional
from .base import BaseTool

class ToolRegistry:
    """Registry managing all tools available to INO Agent."""
    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> BaseTool:
        """Register a tool instance."""
        self._tools[tool.name] = tool
        return tool

    def get_tool(self, name: str) -> Optional[BaseTool]:
        """Get tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> List[BaseTool]:
        """List all registered tools."""
        return list(self._tools.values())

    def get_openai_tools(self) -> List[Dict[str, Any]]:
        """Export all tools in OpenAI tools format."""
        return [t.to_openai_schema() for t in self._tools.values()]

    def execute(self, name: str, arguments: Any) -> str:
        """Execute a tool safely and return string result."""
        tool_obj = self.get_tool(name)
        if not tool_obj:
            return f"Error: Tool '{name}' not found in registry."

        if isinstance(arguments, str):
            try:
                args_dict = json.loads(arguments) if arguments.strip() else {}
            except Exception:
                args_dict = {"query": arguments} if "query" in tool_obj.parameters.get("properties", {}) else {}
        elif isinstance(arguments, dict):
            args_dict = arguments
        else:
            args_dict = {}

        try:
            result = tool_obj.execute(**args_dict)
            if isinstance(result, (dict, list)):
                return json.dumps(result, ensure_ascii=False, indent=2)
            return str(result)
        except Exception as e:
            return f"Error executing tool '{name}': {str(e)}"

    def get_tools_prompt_description(self) -> str:
        """Generate OpenClaw style specification of available tools for prompt inclusion."""
        lines = []
        for t in self._tools.values():
            props = t.parameters.get("properties", {})
            param_list = []
            for p_name, p_info in props.items():
                p_desc = p_info.get("description", "")
                param_list.append(f'"{p_name}": <{p_info.get("type", "string")}>')
            param_json = "{" + ", ".join(param_list) + "}" if param_list else "{}"
            lines.append(f"- **{t.name}**: {t.description}\n  参数格式: {param_json}")
        return "\n".join(lines)

