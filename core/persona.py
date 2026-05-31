"""
主持人配置（Persona）

每个主持人有一个 JSON 文件，目前最小字段：name, gender。
未来可加任意属性（年龄、风格描述、口头禅等），prompt 会自动带上所有字段。

示例 hosts/guopeng.json:
    {
        "name": "Goupeng",
        "gender": "male"
    }
"""

import json
from pathlib import Path
from typing import Any, Dict


class Persona:
    """主持人配置。字段开放，只保留必填的 name。"""
    
    def __init__(self, data: Dict[str, Any]):
        if "name" not in data:
            raise ValueError("主持人 JSON 必须包含 'name' 字段")
        self._data = dict(data)
    
    @classmethod
    def from_file(cls, path: str) -> "Persona":
        with open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f))
    
    @property
    def name(self) -> str:
        return self._data["name"]
    
    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)
    
    def as_dict(self) -> Dict[str, Any]:
        """返回所有字段（用于喂给 LLM prompt）"""
        return dict(self._data)
    
    def __repr__(self):
        return f"Persona({self._data})"