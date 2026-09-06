import json
import os

class CharacterParser:
    def __init__(self, card_path: str):
        self.card_path = card_path

    def load_character(self) -> dict:
        if not os.path.exists(self.card_path):
            return {}
        
        with open(self.card_path, 'r', encoding='utf-8') as f:
            try:
                data = json.load(f)
                return data
            except json.JSONDecodeError:
                print(f"Error parsing {self.card_path}")
                return {}

    def get_system_prompt(self) -> str:
        data = self.load_character()
        if not data:
            return "You are a helpful and friendly AI companion."

        # Support SillyTavern V2 Character schema
        char_data = data.get("data", data)
        
        system_prompt = char_data.get("system_prompt", "").strip()

        # If the card has a well-written system_prompt, use it directly
        # without wrapping it in English labels that confuse small local models.
        if system_prompt:
            # Append example messages as few-shot demonstrations if available
            mes_example = char_data.get("mes_example", "").strip()
            if mes_example:
                # Replace template variables
                name = char_data.get("name", "Companion")
                mes_example = mes_example.replace("{{char}}", name)
                mes_example = mes_example.replace("{{user}}", "主人")
                system_prompt += f"\n\n【對話範例（僅供參考語氣，不要逐字複製）】\n{mes_example}"
            return system_prompt

        # Fallback: build prompt from individual fields (legacy cards)
        name = char_data.get("name", "Companion")
        description = char_data.get("description", "")
        personality = char_data.get("personality", "")
        scenario = char_data.get("scenario", "")
        mes_example = char_data.get("mes_example", "")

        prompt = f"你是{name}。\n\n"
        if description:
            prompt += f"{description}\n\n"
        if personality:
            prompt += f"性格：{personality}\n\n"
        if scenario:
            prompt += f"場景：{scenario}\n\n"
        if mes_example:
            mes_example = mes_example.replace("{{char}}", name).replace("{{user}}", "主人")
            prompt += f"對話範例：\n{mes_example}\n\n"
        
        return prompt
