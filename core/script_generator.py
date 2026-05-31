"""
ScriptGenerator: 调用 LLM 生成主持人台词

支持 OpenAI 兼容的 API（LM Studio / Ollama / OpenAI / 任何兼容端点）。
切换 provider 只需要改 base_url，代码不变。

输入：
    - 主持人 Persona
    - unannouncedSongs（已播但未介绍的歌曲列表）
    - 下一首即将播放的歌（可选）
    - 情境信息（开场？过场？等等）

输出：
    GeneratedScript {
        text: str,              # 生成的台词
        announced_tracks: List[Track]   # 这次台词介绍了哪些歌（用于从 unannounced 移除）
    }

为什么要让 LLM 告诉我们 announced_tracks？因为台词里"介绍了哪些歌"是个 NLP 判断，
让 LLM 自己列出来比我们去字符串匹配可靠。LLM 输出结构化 JSON，再解析。
"""

import os
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Literal

from openai import OpenAI

from .music_controller import Track
from .persona import Persona
from .broadcast_time import BroadcastTimeProvider


SituationKind = Literal["opening", "between_songs", "after_time_signal"]


@dataclass
class GeneratedScript:
    """LLM 生成的一段台词及其元数据"""
    text: str
    announced_tracks: List[Track] = field(default_factory=list)
    
    def __repr__(self):
        return f"GeneratedScript(text={self.text!r}, announced={len(self.announced_tracks)})"


class ScriptGenerator:
    """
    调用 LLM 生成主持人口播。
    
    用法:
        gen = ScriptGenerator(
            base_url="http://localhost:11434/v1",   # Ollama
            api_key="ollama",
            model="qwen2.5:7b-instruct",
            persona=Persona.from_file("hosts/guopeng.json"),
            persona_prompt_file="prompts/host_persona.md",
        )
        script = gen.generate(
            unannounced=[track_a],
            next_song=track_b,
            situation="between_songs",
        )
    """
    
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        persona: Persona,
        persona_prompt_file: str,
        temperature: float = 0.85,
        max_tokens: int = 1000,  # 推理模型要给够，非推理模型其实够用
        timeout: float = 120.0,
        time_provider: Optional["BroadcastTimeProvider"] = None,
        slogans: Optional[List[str]] = None,
    ):
        # 环境变量展开 ${VAR_NAME}
        if api_key.startswith("${") and api_key.endswith("}"):
            env_name = api_key[2:-1]
            api_key = os.environ.get(env_name, "")
        # 某些本地 server 不校验 key，给个占位符就行
        if not api_key:
            api_key = "local-no-key"
        
        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        self.model = model
        self.persona = persona
        self.persona_prompt = Path(persona_prompt_file).read_text(encoding="utf-8")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.time_provider = time_provider
        # slogans：每次 generate 随机挑一条强制 LLM 用作收尾
        self.slogans = list(slogans) if slogans else []
        # 调试统计：最近一次调用耗时（秒）
        self.last_call_elapsed: Optional[float] = None
    
    def health_check(self) -> tuple[bool, str]:
        """
        探活：发一个极小请求，确认 base_url 上的 LLM 服务能用、model 存在。
        返回 (ok, msg)。失败不抛异常。
        """
        import time as _time
        try:
            t0 = _time.monotonic()
            self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "ok"}],
                max_tokens=4,
                temperature=0.0,
            )
            elapsed = _time.monotonic() - t0
            return True, f"LLM 可用（{self.model}，探活耗时 {elapsed:.1f}s）"
        except Exception as e:
            return False, f"LLM 不可用：{e}"
    
    def warmup(self) -> None:
        """
        预热：发一个真实大小的请求，让本地 LLM（Ollama/LM Studio）把模型权重加载到内存。
        本地模型冷启动可能 10-30s，提前 warmup 能让真正的第一次生成快很多。
        失败不抛异常，只是打印警告。
        """
        import time as _time
        try:
            t0 = _time.monotonic()
            self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "你是一个简短回复的助手"},
                    {"role": "user", "content": "热身一下，回个'好'就行"},
                ],
                max_tokens=8,
                temperature=0.0,
            )
            elapsed = _time.monotonic() - t0
            print(f"[script_gen] warmup 完成，耗时 {elapsed:.1f}s")
        except Exception as e:
            print(f"[script_gen] warmup 失败（继续运行）：{e}")
    
    def generate(
        self,
        unannounced: List[Track],
        next_song: Optional[Track] = None,
        situation: SituationKind = "between_songs",
        extra_context: str = "",
        time_offset_seconds: float = 0.0,
    ) -> GeneratedScript:
        """
        生成一段口播。
        
        unannounced: 已播放但未介绍过的歌曲列表
        next_song: 即将播放的下一首歌（None 表示不引出下一首）
        situation: 当前情境
        extra_context: 额外要喂给 LLM 的上下文（比如时间、季节等）
        time_offset_seconds: 时间偏移——用于预生成场景，台词里的"现在"应该是
            "歌结束、口播真正开播的那一刻"，所以传入"歌剩余秒数"即可。
        """
        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(
            unannounced, next_song, situation, extra_context, time_offset_seconds
        )
        
        import time as _time
        t0 = _time.monotonic()
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        self.last_call_elapsed = _time.monotonic() - t0
        
        raw = response.choices[0].message.content or ""
        return self._parse_response(raw, unannounced, next_song)
    
    def _build_system_prompt(self) -> str:
        """主 prompt = 通用风格 + 主持人个人信息"""
        persona_lines = ["## 主持人信息"]
        for k, v in self.persona.as_dict().items():
            persona_lines.append(f"- {k}: {v}")
        persona_block = "\n".join(persona_lines)
        
        # 让 LLM 用 JSON 输出，便于解析 announced_tracks
        output_format = """
## 输出格式（必须严格遵守）

只输出一个 JSON 对象，不要任何 Markdown 标记、解释、前后文。格式如下：

{
  "text": "台词文本",
  "announced": [
    {"name": "歌名", "artist": "艺人"}
  ]
}

字段说明：
- text: 主持人朗读的完整台词
- announced: 这段台词里介绍了哪些歌曲。**只把 unannounced 列表里被你介绍到的填进去**；
  如果你只是引出 next_song（即将播放的歌），它**不**算 announced。
  如果一首都没介绍，给空数组 []
"""
        return f"{self.persona_prompt}\n\n{persona_block}\n\n{output_format}"
    
    # 用于在歌名/专辑名里识别"合作艺人"的关键词。
    # 圆括号/方括号/中文括号里出现这些词时，括号内的人名要被并入艺人字段。
    # 注意 \s* 而不是 \s+——某些音源会写成 "feat.Sia" 没空格。
    _FEAT_PATTERNS = re.compile(
        r"(?:feat\.|featuring|ft\.|with)\s*",
        re.IGNORECASE,
    )
    # 多人之间的分隔符：逗号、& 、" and "、" 和 "、" 與 "、" 跟 "
    _ARTIST_SPLIT_RE = re.compile(
        r"\s*(?:,|&|\band\b|和|與|跟)\s*",
        re.IGNORECASE,
    )
    # 各种括号
    _BRACKET_RE = re.compile(r"[（(\[【]([^（()\[\]【】]*)[)）\]】]")
    
    @classmethod
    def _spoken_title(cls, raw: str) -> str:
        """
        返回适合口播朗读的标题——把所有括号内容删掉。
        合作艺人信息不在这里处理（由 _spoken_track 那级把 feat. 并入艺人）。
        """
        if not raw:
            return ""
        cleaned = cls._BRACKET_RE.sub("", raw)
        # 去除可能残留的多余空格
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        # 去除尾部孤立的标点
        cleaned = cleaned.rstrip(" -–—")
        return cleaned
    
    @classmethod
    def _extract_featured_artists(cls, raw_title: str) -> List[str]:
        """
        从原始歌名里抽取合作艺人。比如：
          "Fortnight (feat. Post Malone)" → ["Post Malone"]
          "Stay (with Justin Bieber)" → ["Justin Bieber"]
          "Song (feat. Sia, Diplo & Labrinth)" → ["Sia", "Diplo", "Labrinth"]
          "Song (feat. Sia, Diplo, & Labrinth)" → ["Sia", "Diplo", "Labrinth"] (Oxford 逗号)
          "Levitating (feat. DaBaby) (Remastered)" → ["DaBaby"]
        没有合作艺人就返回空列表。
        """
        if not raw_title:
            return []
        featured = []
        for m in cls._BRACKET_RE.finditer(raw_title):
            inside = m.group(1).strip()
            feat_match = cls._FEAT_PATTERNS.search(inside)
            if feat_match:
                # 取关键词之后的部分作为艺人列表
                names_part = inside[feat_match.end():].strip()
                # 按分隔符切，空字符串过滤掉
                for name in cls._ARTIST_SPLIT_RE.split(names_part):
                    name = name.strip()
                    if name:
                        featured.append(name)
        return featured
    
    @staticmethod
    def _join_artists(main: str, featured: List[str]) -> str:
        """
        把主艺人和合作艺人拼成一个适合 LLM 朗读的字符串。
        
        策略：
        - 没合作艺人 → 只返回主艺人
        - 1 位合作 → "Main & Feat"
        - 2 位+合作 → "Main, F1, F2 & F3"（英文 Oxford 风格，最后一个用 &）
        
        这样念起来比一连串 & 自然得多。
        """
        if not featured:
            return main
        all_names = [main] + featured
        if len(all_names) == 2:
            return f"{all_names[0]} & {all_names[1]}"
        # 3 位+：前面用逗号，最后一个用 &
        return ", ".join(all_names[:-1]) + f" & {all_names[-1]}"
    
    @classmethod
    def _spoken_track(cls, track: Track) -> dict:
        """
        把 Track 转成"给 LLM 的清洗版"信息字典：
        - spoken_name: 歌名去掉所有括号内容
        - spoken_artist: 主艺人 + (如果歌名里有 feat. 标记) 合作艺人，用自然连接
        - spoken_album: 专辑名去掉所有括号内容
        """
        spoken_name = cls._spoken_title(track.name)
        spoken_album = cls._spoken_title(track.album) if track.album else ""
        featured = cls._extract_featured_artists(track.name)
        spoken_artist = cls._join_artists(track.artist, featured)
        
        return {
            "name": spoken_name,
            "artist": spoken_artist,
            "album": spoken_album,
            "year": track.year,
        }
    
    def _build_user_prompt(
        self,
        unannounced: List[Track],
        next_song: Optional[Track],
        situation: SituationKind,
        extra_context: str,
        time_offset_seconds: float = 0.0,
    ) -> str:
        parts = []
        
        # 当前广播时间（含时区、季节）—— 主持人会拿来报时和聊天气
        # offset 用于预生成：台词里的"现在"应反映"口播真正开播那一刻"，
        # 而不是"调用 generate() 那一刻"——两者差着一首歌的时长。
        if self.time_provider is not None:
            parts.append(self.time_provider.now(offset_seconds=time_offset_seconds).as_prompt_block())
        
        if situation == "opening":
            parts.append("**情境**：节目开场，简短问候听众，然后引出第一首歌。")
        elif situation == "after_time_signal":
            parts.append("**情境**：刚刚整点报时结束，简短回归节目，引出下一首歌。")
        else:
            parts.append("**情境**：歌曲之间的过场白。")
        
        if unannounced:
            ua_lines = ["**刚刚播放过但还没被主持人介绍的歌曲（unannouncedSongs）**："]
            for t in unannounced:
                s = self._spoken_track(t)
                line = f"- 《{s['name']}》 by {s['artist']}"
                if s["album"]:
                    line += f"（专辑《{s['album']}》"
                    if s["year"]:
                        line += f"，{s['year']}"
                    line += "）"
                ua_lines.append(line)
            ua_lines.append(
                "\n**铁律**：台词第一句必须就报到上面所有的歌，按列表顺序，"
                "用 \"By [艺人] [歌名]\" 或 \"[艺人] [歌名]\" 的倒装句式。"
                "不要先报时、寒暄、聊天——所有那些都必须放在报歌之后。"
                "**严格使用上面给出的歌名/艺人/专辑名原文**，不要加任何括号注释、"
                "版本说明、年份后缀。如果歌名里有 feat. 合作艺人，已经在 artist "
                "字段里给你了，按 artist 字段读即可。"
                "把这些歌都填到 JSON 的 announced 字段里（announced 字段里的 name "
                "和 artist 也按这里给的清洗版填）。"
            )
            parts.append("\n".join(ua_lines))
        else:
            parts.append("**unannouncedSongs 是空的**——这次台词不需要回顾过去的歌。")
        
        if next_song:
            sn = self._spoken_track(next_song)
            ns = f"**下一首即将播放的歌**：《{sn['name']}》 by {sn['artist']}"
            if sn["album"]:
                ns += f"（专辑《{sn['album']}》"
                if sn["year"]:
                    ns += f"，{sn['year']}"
                ns += "）"
            ns += (
                "\n\n**硬性要求**：你必须在台词结尾自然地引出这首歌——"
                "可以详细介绍（艺人背景、歌曲故事），也可以简短带出（比如\"接下来送给你的是…\"），"
                "但**一定要让听众明确知道下一首是什么**。**严格使用上面给出的歌名/艺人原文**，"
                "不要加任何括号注释。**这首歌不算 announced**，不要填进 announced 列表。"
            )
            parts.append(ns)
        else:
            parts.append("**没有下一首歌信息**——你不需要引出下一首，台词以一个自然的收尾结束即可。")
        
        if extra_context:
            parts.append(f"**额外信息**：\n{extra_context}")
        
        # Slogan 指令：程序选好哪一条，LLM 不能改写、不能翻译
        if self.slogans:
            import random as _rand
            chosen_slogan = _rand.choice(self.slogans)
            parts.append(
                f"**Slogan 铁律**：本次台词的最后一句必须是 "
                f"\"{chosen_slogan}\" ——**严格按这个英文原文写**，"
                f"不要翻译成中文，不要改写、不要加修饰词。如果你已经先引出了下一首歌，"
                f"那么 slogan 紧跟在引出之后即可。例如：\"...接下来送给你的是 Espresso，"
                f"by Sabrina Carpenter，{chosen_slogan}。\""
            )
        
        parts.append("现在请输出 JSON。")
        return "\n\n".join(parts)
    
    def _parse_response(
        self,
        raw: str,
        unannounced: List[Track],
        next_song: Optional[Track],
    ) -> GeneratedScript:
        """
        从 LLM 输出里提取 text 和 announced 列表。
        
        因为 LLM 偶尔会带 Markdown 围栏（```json...```）或额外解释，要兼容这些。
        本地小模型经常输出非法 JSON 转义（如 \\'），加一层修复 + 文本回填。
        """
        # 1. 尝试找出 JSON 部分
        json_text = self._extract_json(raw)
        
        parsed = None
        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError as first_err:
            # 尝试修常见错：本地小模型经常把单引号写成 \' （非法 JSON 转义）
            repaired = self._repair_common_json_errors(json_text)
            try:
                parsed = json.loads(repaired)
                print("[script_gen] 注意：原始 JSON 有非法转义，已自动修复")
            except json.JSONDecodeError:
                print(
                    f"[script_gen] 警告：LLM 输出不是合法 JSON，把原文当台词。\n"
                    f"  错误: {first_err}\n"
                    f"  原文: {raw[:200]}"
                )
                # 把整个原文当台词；announced 通过文本回填
                text_fallback = raw.strip()
                # 如果原文以 { 开头，可能是 JSON 残骸，至少尝试摘出 text 字段
                text_inner = self._extract_text_field_loose(raw)
                if text_inner:
                    text_fallback = text_inner
                return GeneratedScript(
                    text=text_fallback,
                    announced_tracks=self._infer_announced_from_text(text_fallback, unannounced),
                )
        
        text = parsed.get("text", "").strip()
        if not text:
            print(f"[script_gen] 警告：LLM 输出的 text 为空。原 JSON: {parsed}")
            return GeneratedScript(text="", announced_tracks=[])
        
        # 2. 把 announced 字段映射回原始 Track 对象。
        # LLM 看到的是清洗版（无括号、feat. 合作艺人并入），所以匹配时
        # 也要用 _spoken_track 把候选 Track 转成清洗版后再比对。
        announced_tracks = []
        for item in parsed.get("announced", []):
            if not isinstance(item, dict):
                continue
            name = item.get("name", "")
            artist = item.get("artist", "")
            matched = self._match_track(name, artist, unannounced)
            if matched and matched not in announced_tracks:
                announced_tracks.append(matched)
        
        # 3. 文本回填：如果 LLM 在 text 里实际提到了某首歌但没列入 announced 字段，
        # 补上——它已经介绍了，下次不该再介绍。
        text_inferred = self._infer_announced_from_text(text, unannounced)
        for t in text_inferred:
            if t not in announced_tracks:
                announced_tracks.append(t)
        
        return GeneratedScript(text=text, announced_tracks=announced_tracks)
    
    @classmethod
    def _match_track(cls, name: str, artist: str, candidates: List[Track]) -> Optional[Track]:
        """
        给定 LLM 输出里的 name + artist（已是清洗版），在 candidates 里找匹配的原始 Track。
        匹配优先级：
        1. 原始字段严格相等
        2. 清洗版 name 相等（artist 可能因为加了 feat. 而不严格相等，name 更可靠）
        3. 清洗版 + 归一化（去空格小写）name 相等
        """
        if not name:
            return None
        norm = lambda s: s.strip().lower().replace(" ", "")
        
        # 1. 严格相等
        for t in candidates:
            if t.name == name and t.artist == artist:
                return t
        
        # 2. 清洗版 name 相等
        name_norm = norm(name)
        for t in candidates:
            spoken = cls._spoken_track(t)
            if norm(spoken["name"]) == name_norm:
                return t
        
        # 3. 原始 name 归一化后相等（兜底：LLM 可能直接照搬原始 name）
        for t in candidates:
            if norm(t.name) == name_norm:
                return t
        
        return None
    
    @staticmethod
    def _repair_common_json_errors(s: str) -> str:
        """
        修常见的 LLM JSON 错误：
        - 非法转义 \\'  →  '
        - 非法转义 \\&  →  &
        其他真正合法的转义（\\n、\\"、\\\\、\\u..等）原样保留。
        """
        # 用正则匹配「\后面跟一个非合法转义符」，把反斜杠去掉
        # 合法的 JSON 转义符是：" \ / b f n r t u
        return re.sub(r'\\([^"\\/bfnrtu])', r'\1', s)
    
    @staticmethod
    def _extract_text_field_loose(raw: str) -> str:
        """
        从坏掉的 JSON 残骸里宽松提取 "text" 字段的值。
        比纯 json.loads 容忍非法转义。
        """
        # 匹配  "text": "..."  ，引号里允许任何字符（包括非法转义符）
        m = re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', raw, re.DOTALL)
        if not m:
            return ""
        s = m.group(1)
        # 简单 unescape：把 \" → "，\\ → \，\n → 换行
        s = s.replace('\\"', '"').replace("\\n", "\n").replace("\\'", "'")
        return s.strip()
    
    @classmethod
    def _infer_announced_from_text(cls, text: str, unannounced: List[Track]) -> List[Track]:
        """
        扫描台词文本，找出实际被介绍过的 unannounced 歌曲。
        
        LLM 拿到的是清洗版歌名（无括号），所以匹配规则：
        只要"清洗版"歌名（去引号去空格小写）在文本里出现，就认为被介绍了。
        """
        if not text or not unannounced:
            return []
        
        norm = lambda s: s.strip().lower().replace(" ", "")
        text_norm = norm(text)
        result = []
        for t in unannounced:
            spoken_name = cls._spoken_track(t)["name"]
            name_norm = norm(spoken_name)
            if not name_norm:
                continue
            if name_norm in text_norm:
                result.append(t)
        return result
    
    @staticmethod
    def _extract_json(raw: str) -> str:
        """尝试从 LLM 输出里抠出 JSON 部分"""
        raw = raw.strip()
        
        # Case 1: Markdown 围栏 ```json ... ```
        m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
        if m:
            return m.group(1).strip()
        
        # Case 2: 整段就是 JSON
        if raw.startswith("{") and raw.endswith("}"):
            return raw
        
        # Case 3: JSON 嵌在某段文本里，找第一个 { 到最后一个 }
        first = raw.find("{")
        last = raw.rfind("}")
        if first >= 0 and last > first:
            return raw[first:last + 1]
        
        return raw