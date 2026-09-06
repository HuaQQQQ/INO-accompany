import inspect
import json
from typing import Any, Callable, Dict, Optional, Type, get_type_hints

class BaseTool:
    """Base class for all Agent Tools."""
    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = {}

    def __init__(self, name: str = "", description: str = "", parameters: Optional[Dict[str, Any]] = None):
        if name:
            self.name = name
        if description:
            self.description = description
        if parameters is not None:
            self.parameters = parameters

    def execute(self, **kwargs) -> Any:
        raise NotImplementedError("Tool execute method must be implemented.")

    def to_openai_schema(self) -> Dict[str, Any]:
        """Convert tool to standard OpenAI tools function format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        }

    def __repr__(self):
        return f"<Tool: {self.name}>"


def _python_type_to_json_type(py_type: Type) -> str:
    if py_type in (int, float):
        return "number"
    if py_type is bool:
        return "boolean"
    if py_type is list:
        return "array"
    if py_type is dict:
        return "object"
    return "string"


class FunctionTool(BaseTool):
    def __init__(self, func: Callable, name: Optional[str] = None, description: Optional[str] = None):
        self.func = func
        tool_name = name or func.__name__
        tool_desc = description or (inspect.getdoc(func) or f"Execute {tool_name}").strip()
        
        # Build JSON Schema parameters from function signature
        sig = inspect.signature(func)
        type_hints = get_type_hints(func) if hasattr(func, '__annotations__') else {}
        
        properties = {}
        required = []
        
        for param_name, param in sig.parameters.items():
            if param_name in ('self', 'cls'):
                continue
            
            param_type = type_hints.get(param_name, str)
            json_type = _python_type_to_json_type(param_type)
            
            param_desc = f"Parameter {param_name}"
            # Extract docstring param descriptions if available
            properties[param_name] = {
                "type": json_type,
                "description": param_desc
            }
            
            if param.default == inspect.Parameter.empty:
                required.append(param_name)
        
        parameters = {
            "type": "object",
            "properties": properties,
            "required": required
        }
        
        super().__init__(name=tool_name, description=tool_desc, parameters=parameters)

    def execute(self, **kwargs) -> Any:
        return self.func(**kwargs)


def tool(name: Optional[str] = None, description: Optional[str] = None):
    """Decorator to convert a standard Python function into an Agent Tool."""
    def decorator(func: Callable) -> FunctionTool:
        return FunctionTool(func, name=name, description=description)
    return decorator
