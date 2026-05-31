"""
文案生成器

用 OpenAI 兼容格式调用 LLM，这样 MiniMax / LM Studio / Ollama / OpenAI 都能用同一套代码。
切换只需要改 config.yaml 里的 provider 和 base_url。

注意：MiniMax 有自己的 API 格式，但也提供 OpenAI 兼容端点。
如果 MiniMax 的兼容层有问题，后面可以加一个 MiniMaxAdapter 处理。
"""

import os
import json
from pathlib import Path
from typing import Optional
from openai import OpenAI

from .music_controller import Track
from .metadata import EnrichedMetadata, format_for_prompt


class ScriptWriter:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        persona_file: str,
        style_memory_file: str,
        target_seconds: int = 20,
    ):
        # 展开环境变量，比如 "${MINIMAX_API_KEY}"
        if api_key.startswith("${") and api_key.endswith("}"):
            env_name = api_key[2:-1]
            api_key = os.environ.get(env_name, "")
            if not api_key:
                raise RuntimeError(f"环境变量 {env_name} 未设置")
        
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model
        self.persona = Path(persona_file).read_text(encoding="utf-8")
        self.style_memory_file = style_memory_file
        self.target_seconds = target_seconds
    
    def _load_style_rules(self) -> list:
        """读取用户反馈过的风格偏好"""
        try:
            with open(self.style_memory_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("rules", [])
        except Exception:
            return []
    
    def add_style_rule(self, rule: str):
        """追加一条风格规则到 memory"""
        try:
            with open(self.style_memory_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {"rules": []}
        
        data.setdefault("rules", []).append(rule)
        with open(self.style_memory_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    def write_script(
        self,
        track: Track,
        enriched: Optional[EnrichedMetadata] = None,
        is_opening: bool = False,
        previous_track: Optional[Track] = None,
    ) -> str:
        """
        生成过场白文案。
        
        is_opening: 是否是整个电台的开场（第一首歌）
        previous_track: 上一首歌，用于承接过渡
        """
        rules = self._load_style_rules()
        rules_text = ""
        if rules:
            rules_text = "\n\n## 用户反馈的额外风格要求\n\n" + "\n".join(f"- {r}" for r in rules)
        
        context_lines = [
            f"歌曲: 《{track.name}》",
            f"艺人: {track.artist}",
            f"专辑: {track.album}",
        ]
        if track.year:
            context_lines.append(f"年份: {track.year}")
        if track.genre:
            context_lines.append(f"流派: {track.genre}")
        
        context = "\n".join(context_lines)
        enriched_text = format_for_prompt(enriched) if enriched else "（暂无额外资料）"
        
        situation = ""
        if is_opening:
            situation = "\n**当前情境**：这是本期节目的开场第一首，需要简短问候一下听众。"
        elif previous_track:
            situation = f"\n**当前情境**：上一首刚放完《{previous_track.name}》- {previous_track.artist}，你可以顺势过渡。"
        
        system_prompt = self.persona + rules_text
        user_prompt = f"""请为即将播放的歌曲写一段 DJ 过场白。

## 歌曲信息

{context}

## 补充背景资料

{enriched_text}
{situation}

## 时长要求

朗读时长约 {self.target_seconds} 秒（中文约 {self.target_seconds * 4}-{self.target_seconds * 5} 字）。

直接输出文案本身，不要引号、不要标题、不要解释。"""

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.85,  # 文案需要一点创造性
            max_tokens=1200,
        )
        
        script = response.choices[0].message.content.strip()
        # 去掉模型偶尔会加的引号
        if script.startswith(("「", "\"", "『", "“")) and script.endswith(("」", "\"", "』", "”")):
            script = script[1:-1].strip()
        return script
