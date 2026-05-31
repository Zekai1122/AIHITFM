"""
HITFM Local - 主入口

用法:
    # 1. 确保 Music.app 已打开，选好播放列表并开始播放
    # 2. 设置 MiniMax API key:
    #    export MINIMAX_API_KEY="your-key"
    # 3. 运行:
    #    python main.py
"""

import sys
import yaml
from pathlib import Path

from core.music_controller import AppleMusicController
from core.script_writer import ScriptWriter
from core.tts import build_tts
from core.dj import DJ


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    config = load_config()
    
    # 1. LLM
    llm_cfg = config["llm"]
    provider = llm_cfg["provider"]
    provider_cfg = llm_cfg[provider]
    
    writer = ScriptWriter(
        base_url=provider_cfg["base_url"],
        api_key=provider_cfg["api_key"],
        model=provider_cfg["model"],
        persona_file=config["dj"]["persona_file"],
        style_memory_file=config["dj"]["style_memory_file"],
        target_seconds=config["dj"]["target_script_seconds"],
    )
    
    # 2. TTS
    tts = build_tts(config["tts"])
    
    # 3. Music 控制器
    music = AppleMusicController()
    
    # 4. 启动 DJ
    dj = DJ(
        music=music,
        writer=writer,
        tts=tts,
        enable_metadata_fetch=config["metadata"]["enable_musicbrainz"],
    )
    
    try:
        dj.run_demo(max_songs=config["demo"]["max_songs"])
    except KeyboardInterrupt:
        print("\n[DJ] 被中断，再见")
        sys.exit(0)


if __name__ == "__main__":
    main()
