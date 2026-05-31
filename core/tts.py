"""
TTS 抽象层

两个实现：
- SayTTS: macOS 自带的 `say` 命令，零依赖，用于跑通流程
- IndexTTSHTTPTTS: 调用本地 IndexTTS-2 HTTP API 服务（需要另外启动 api_server.py）

切换只需要改 config.yaml 的 tts.provider 字段。
"""

import subprocess
import tempfile
import os
import urllib.request
import urllib.error
import json
from abc import ABC, abstractmethod
from typing import Optional


class TTSBase(ABC):
    """TTS 接口：调用方只需要 speak() 一个方法"""
    
    def speak(self, text: str) -> None:
        """合成并播放文本，阻塞到播完。"""
        return self.synthesize_and_play(text)
    
    @abstractmethod
    def synthesize_and_play(self, text: str) -> None:
        pass
    
    @abstractmethod
    def synthesize_to_file(self, text: str, output_path: str) -> None:
        pass
    
    def health_check(self) -> tuple[bool, str]:
        """返回 (是否可用, 状态描述)"""
        return True, "OK"
    
    def is_warmup_done(self) -> bool:
        """是否已对当前配置完成预热。基类默认 True（say 这种不需要预热）"""
        return True


class SayTTS(TTSBase):
    """macOS 自带的 say 命令，零依赖"""
    
    def __init__(self, voice: str = "Tingting", rate: int = 180):
        self.voice = voice
        self.rate = rate
    
    def synthesize_and_play(self, text: str) -> None:
        subprocess.run(
            ["say", "-v", self.voice, "-r", str(self.rate), text],
            check=True,
        )
    
    def synthesize_to_file(self, text: str, output_path: str) -> None:
        subprocess.run(
            ["say", "-v", self.voice, "-r", str(self.rate), "-o", output_path, text],
            check=True,
        )


class IndexTTSHTTPTTS(TTSBase):
    """
    IndexTTS-2 的 HTTP 客户端。
    
    前置条件：
    - 在 index-tts 项目目录里启动了 api_server.py:
        cd ~/Documents/Coding/index-tts
        uv run python api_server.py
    - 参考音色音频已准备好
    
    使用示例:
        tts = IndexTTSHTTPTTS(
            api_url="http://127.0.0.1:9881",
            ref_audio_path="/path/to/dj.wav",
        )
        tts.speak("嗨，这里是HIT FM")
    """
    
    def __init__(
        self,
        api_url: str = "http://127.0.0.1:9881",
        ref_audio_path: str = "",
        # emotion 相关，默认全不指定，让 IndexTTS 自己从文本推断
        emo_audio_prompt: Optional[str] = None,
        emo_alpha: float = 1.0,
        emo_text: Optional[str] = None,
        use_emo_text: bool = False,
        use_random: bool = False,
        verbose: bool = False,
        max_text_tokens_per_segment: int = 120,
        timeout: int = 300,  # IndexTTS-2 比 GPT-SoVITS 慢，超时给够
    ):
        self.api_url = api_url.rstrip("/")
        self.ref_audio_path = ref_audio_path
        self.emo_audio_prompt = emo_audio_prompt
        self.emo_alpha = emo_alpha
        self.emo_text = emo_text
        self.use_emo_text = use_emo_text
        self.use_random = use_random
        self.verbose = verbose
        self.max_text_tokens_per_segment = max_text_tokens_per_segment
        self.timeout = timeout
    
    def health_check(self) -> tuple[bool, str]:
        """检查 API 服务是否在跑"""
        try:
            req = urllib.request.Request(f"{self.api_url}/health")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == "ok" and data.get("model_loaded"):
                return True, "API 服务正常，模型已加载"
            else:
                return False, f"API 状态异常: {data}"
        except urllib.error.URLError as e:
            return False, f"连不上 {self.api_url}，IndexTTS API 服务可能没启动: {e.reason}"
        except Exception as e:
            return False, f"健康检查失败: {e}"
    
    def is_warmup_done(self) -> bool:
        """
        查询服务端是否已完成 warmup。
        
        信任服务端返回的 warmup_done 字段。如果你需要严格保证 warmup 用的是
        当前这个参考音频，在启动 api_server 时通过 --warmup-ref-audio 指定同一个
        路径，两边配置一致就行。
        
        网络异常时返回 False（保守起见，宁可走等待状态也不崩）。
        """
        try:
            req = urllib.request.Request(f"{self.api_url}/health")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return False
        
        return bool(data.get("warmup_done", False))
    
    def _check_ref_audio(self) -> None:
        if not self.ref_audio_path:
            raise RuntimeError("IndexTTSHTTPTTS 未配置 ref_audio_path")
        # 注意：ref_audio_path 是**服务端**的路径，客户端不验证文件存在
        # 因为 HITFM 和 IndexTTS API 可能在不同进程，甚至理论上不同机器
    
    def synthesize_to_file(self, text: str, output_path: str) -> None:
        self._check_ref_audio()
        
        payload = {
            "text": text,
            "ref_audio_path": self.ref_audio_path,
            "emo_audio_prompt": self.emo_audio_prompt,
            "emo_alpha": self.emo_alpha,
            "emo_text": self.emo_text,
            "use_emo_text": self.use_emo_text,
            "use_random": self.use_random,
            "verbose": self.verbose,
            "max_text_tokens_per_segment": self.max_text_tokens_per_segment,
        }
        
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.api_url}/tts",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                audio_data = resp.read()
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8")
            except Exception:
                err_body = "(无法读取响应体)"
            raise RuntimeError(f"IndexTTS 合成失败 (HTTP {e.code}): {err_body}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"连不上 IndexTTS API: {e.reason}")
        
        with open(output_path, "wb") as f:
            f.write(audio_data)
    
    def synthesize_and_play(self, text: str) -> None:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            self.synthesize_to_file(text, tmp_path)
            subprocess.run(["afplay", tmp_path], check=True)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


def build_tts(config: dict, ref_audio_override: Optional[str] = None) -> TTSBase:
    """
    根据配置字典创建对应的 TTS 实例。
    
    ref_audio_override: 若提供（一般来自 Host.voice_ref_path），覆盖 config 里的
        indextts.ref_audio_path——让 host 目录里的音色文件成为权威来源。
        config 里的 ref_audio_path 仅在没传 override 时作为 fallback。
    """
    provider = config["provider"]
    if provider == "say":
        cfg = config.get("say", {})
        return SayTTS(
            voice=cfg.get("voice", "Tingting"),
            rate=cfg.get("rate", 180),
        )
    elif provider == "indextts":
        cfg = config["indextts"]
        ref_audio_path = ref_audio_override or cfg.get("ref_audio_path", "")
        if not ref_audio_path:
            raise ValueError(
                "IndexTTS 找不到参考音色——请在 hosts/<host>/voice_ref.wav 放音频文件，"
                "或在 config.yaml 的 tts.indextts.ref_audio_path 指定"
            )
        return IndexTTSHTTPTTS(
            api_url=cfg.get("api_url", "http://127.0.0.1:9881"),
            ref_audio_path=ref_audio_path,
            emo_audio_prompt=cfg.get("emo_audio_prompt"),
            emo_alpha=cfg.get("emo_alpha", 1.0),
            emo_text=cfg.get("emo_text"),
            use_emo_text=cfg.get("use_emo_text", False),
            use_random=cfg.get("use_random", False),
            verbose=cfg.get("verbose", False),
            max_text_tokens_per_segment=cfg.get("max_text_tokens_per_segment", 120),
            timeout=cfg.get("timeout", 300),
        )
    else:
        raise ValueError(f"未知的 TTS provider: {provider}（支持 say / indextts）")