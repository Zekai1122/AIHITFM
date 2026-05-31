"""
主持人（Host）

每个主持人是 hosts/ 下的一个文件夹，结构：

    hosts/<host_id>/
        profile.json        必需。主持人元信息（name, gender, 自由字段）
        voice_ref.wav       必需。TTS 参考音色（也支持 .mp3）
        emotion_ref.wav     可选（预留，未来支持情感参考）
        emotion.json        可选（预留，未来支持情感配置）

config.yaml 里通过 `host: <host_id>` 指定使用哪个主持人。

profile.json 至少包含 name 字段；其他字段（gender、age、style、catchphrase 等）
会被原样喂给 LLM prompt，主持人特征完全由用户控制。

为了兼容旧的 Persona 接口，Host 提供 .persona 属性返回一个 Persona 对象。
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional

from .persona import Persona


VOICE_REF_EXTENSIONS = (".wav", ".mp3", ".flac", ".m4a")
EMOTION_REF_EXTENSIONS = (".wav", ".mp3", ".flac", ".m4a")


class Host:
    """主持人——按目录加载，包含 persona + 参考音频 + 可选情感配置。"""
    
    def __init__(
        self,
        host_id: str,
        host_dir: Path,
        persona: Persona,
        voice_ref_path: Path,
        emotion_ref_path: Optional[Path] = None,
        emotion_config: Optional[Dict[str, Any]] = None,
    ):
        self.host_id = host_id
        self.host_dir = host_dir
        self.persona = persona
        self.voice_ref_path = voice_ref_path
        self.emotion_ref_path = emotion_ref_path
        self.emotion_config = emotion_config or {}
    
    @property
    def name(self) -> str:
        """主持人显示名，从 persona 拿"""
        return self.persona.name
    
    @classmethod
    def from_dir(cls, host_id: str, hosts_root: str = "hosts") -> "Host":
        """
        从 hosts/<host_id>/ 加载。
        
        缺失必需文件会抛 FileNotFoundError，给清晰的错误信息让用户知道怎么修。
        """
        host_dir = Path(hosts_root) / host_id
        if not host_dir.is_dir():
            raise FileNotFoundError(
                f"找不到主持人目录: {host_dir}（请确认 hosts/ 下有 '{host_id}' 文件夹）"
            )
        
        # --- 必需：profile.json ---
        profile_path = host_dir / "profile.json"
        if not profile_path.is_file():
            raise FileNotFoundError(
                f"找不到主持人 profile: {profile_path}（必需文件，至少包含 'name' 字段）"
            )
        try:
            persona = Persona.from_file(str(profile_path))
        except Exception as e:
            raise ValueError(f"主持人 profile 加载失败 ({profile_path}): {e}") from e
        
        # --- 必需：voice_ref.* ---
        voice_ref = cls._find_file(host_dir, "voice_ref", VOICE_REF_EXTENSIONS)
        if voice_ref is None:
            raise FileNotFoundError(
                f"找不到主持人参考音频: {host_dir}/voice_ref.{{wav,mp3,flac,m4a}}"
                f"（必需文件，5-10 秒清晰单人讲话）"
            )
        
        # --- 可选：emotion_ref.* + emotion.json ---
        emotion_ref = cls._find_file(host_dir, "emotion_ref", EMOTION_REF_EXTENSIONS)
        emotion_config: Optional[Dict[str, Any]] = None
        emotion_json = host_dir / "emotion.json"
        if emotion_json.is_file():
            try:
                with open(emotion_json, "r", encoding="utf-8") as f:
                    emotion_config = json.load(f)
            except Exception as e:
                print(f"[host] 警告：emotion.json 加载失败（忽略）: {e}")
        
        return cls(
            host_id=host_id,
            host_dir=host_dir,
            persona=persona,
            voice_ref_path=voice_ref.resolve(),  # 绝对路径，便于传给 IndexTTS 服务
            emotion_ref_path=emotion_ref.resolve() if emotion_ref else None,
            emotion_config=emotion_config,
        )
    
    @staticmethod
    def _find_file(directory: Path, stem: str, exts: tuple) -> Optional[Path]:
        """在 directory 下找 stem.<ext>，按 exts 顺序匹配第一个存在的"""
        for ext in exts:
            p = directory / f"{stem}{ext}"
            if p.is_file():
                return p
        return None
    
    def __repr__(self):
        return f"Host(id={self.host_id!r}, name={self.name!r}, voice_ref={self.voice_ref_path})"


def list_available_hosts(hosts_root: str = "hosts") -> list[str]:
    """列出 hosts/ 下所有合法的主持人 id（即包含 profile.json 的子目录）"""
    root = Path(hosts_root)
    if not root.is_dir():
        return []
    result = []
    for sub in sorted(root.iterdir()):
        if sub.is_dir() and (sub / "profile.json").is_file():
            result.append(sub.name)
    return result