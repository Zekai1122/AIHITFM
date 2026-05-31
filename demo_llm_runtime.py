"""
HITFM Local - LLM Runtime Demo

第一个真实的 AI 电台主循环：
    Apple Music + Scheduler + LLM (Ollama) + IndexTTS-2

流程：
1. 加载配置、构造组件（Music / TTS / ScriptGenerator）
2. LLM warmup（让 Ollama 把权重加载好）
3. 检查 Apple Music 是否在播、是否开了随机播放
4. 进入主循环，按 Scheduler 的决策一段一段播：
     STATION_ID → SONG（一开始播就后台启动下一段 host_talk 的预生成）
                → HOST_TALK（直接拿后台预生成结果，必要时垫音掩盖等待）
                → SONG ...

用法：
    python demo_llm_runtime.py
    python demo_llm_runtime.py --max-segments 8
"""

import argparse
import random
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, List

import yaml

from core.music_controller import AppleMusicController, Track
from core.unannounced import UnannouncedSongs
from core.scheduler import Scheduler
from core.script_generator import ScriptGenerator
from core.persona import Persona
from core.host import Host, list_available_hosts
from core.tts import build_tts
from core.prebaked_script import PrebakedScript
from core.bed_music import play_bed_music
from core.waiting_state import WaitingState
from core.broadcast_time import BroadcastTimeProvider
from core.time_signal import TimeSignalChecker


# ==================== 配置常量 ====================

STATION_ID_DIR = "audio/station_id"
# 开场用哪种 station_id：从这些文件里随机选一个（更"开场"感）
OPENING_STATION_ID_PATTERNS = ["station_id_a", "station_id_b", "station_id_c", "station_id_d", "station_id_e"]
# 歌曲中间的 station_id：这些更短促，适合过渡
MID_STATION_ID_PATTERNS = ["station_id_1", "station_id_2", "station_id_jingle", "back_1", "back_2"]

# 歌曲快结束时提前多少秒按 pause——留出余量确保我们抢在 Apple Music 自动切歌之前。
# 太小（0）容易漏出下一首前奏；太大（>1.0）会切掉歌尾。0.5 是经验上的折中。
PAUSE_EARLY_BY = 0.5
BED_MUSIC_VOLUME = 0.05
BED_MUSIC_FADE_OUT = 1.5

# 启动后台预生成时，给它的最长等待时间（秒）—— 主循环里阻塞等结果的上限
PREBAKE_WAIT_TIMEOUT = 120.0


# ==================== 工具 ====================

def afplay(path: str) -> None:
    if not Path(path).exists():
        raise FileNotFoundError(f"音频文件不存在: {path}")
    print(f"[runtime] 播放音频: {Path(path).name}")
    subprocess.run(["afplay", path], check=True)


def pick_station_id_file(patterns: List[str], directory: str = STATION_ID_DIR) -> Optional[str]:
    """从 station_id 目录里按文件名前缀匹配，随机挑一个。"""
    dir_path = Path(directory)
    if not dir_path.exists():
        return None
    candidates = []
    for p in dir_path.iterdir():
        if p.suffix.lower() not in {".mp3", ".wav", ".m4a"}:
            continue
        stem = p.stem
        if any(stem.startswith(pat) for pat in patterns):
            candidates.append(str(p))
    return random.choice(candidates) if candidates else None


def build_components(cfg: dict):
    """根据 config.yaml 构造 Host + ScriptGenerator + TTS + Music + TimeSignal"""
    # --- 广播时间 ---
    time_provider = BroadcastTimeProvider.from_config(cfg.get("time"))
    bt = time_provider.now()
    print(f"[runtime] 时区: {bt.spoken_zone_name} (IANA={bt.iana_zone}, "
          f"{bt.season_zh}, {bt.hemisphere}) → 当前 {bt.hour:02d}:{bt.minute:02d}")
    
    # --- 主持人（Host）---
    host_id = cfg.get("host")
    if not host_id:
        available = list_available_hosts()
        raise ValueError(
            f"config.yaml 缺少 'host' 字段（指定主持人 id）。"
            + (f" 当前可用：{available}" if available else " hosts/ 目录还是空的。")
        )
    try:
        host = Host.from_dir(host_id)
    except FileNotFoundError as e:
        available = list_available_hosts()
        raise FileNotFoundError(
            f"{e}\n  当前可用主持人：{available}" if available else str(e)
        ) from e
    print(f"[runtime] 主持人: {host.name} (id={host.host_id})")
    print(f"[runtime] 参考音色: {host.voice_ref_path}")
    
    # --- LLM ---
    llm_cfg = cfg["llm"]
    provider = llm_cfg["provider"]
    provider_cfg = llm_cfg[provider]
    
    generator = ScriptGenerator(
        base_url=provider_cfg["base_url"],
        api_key=provider_cfg["api_key"],
        model=provider_cfg["model"],
        persona=host.persona,
        persona_prompt_file="prompts/host_persona.md",
        time_provider=time_provider,
        slogans=cfg.get("slogans") or [],
    )
    
    # --- TTS（用 host 的 voice_ref 覆盖 config 里可能的 ref_audio_path）---
    tts = build_tts(cfg["tts"], ref_audio_override=str(host.voice_ref_path))
    
    # --- Apple Music ---
    music = AppleMusicController()
    
    # --- 整点报时 ---
    try:
        time_signal = TimeSignalChecker(time_provider=time_provider, hours_dir="audio/hours")
    except (FileNotFoundError, RuntimeError) as e:
        print(f"[runtime] ⚠ 整点报时不可用（{e}），将跳过该功能")
        time_signal = None
    
    return generator, tts, music, host, time_signal


# ==================== 主循环辅助 ====================

def wait_for_song_to_end(
    music: AppleMusicController,
    track: Track,
    pause_early_by: float = PAUSE_EARLY_BY,
    on_tick=None,
    time_signal_checker=None,  # 可选 TimeSignalChecker，每次轮询时检查整点报时
) -> str:
    """
    等当前曲目播完，然后**立刻** pause。
    
    返回值：
    - "ended_normally": 歌正常播完
    - "interrupted_by_time_signal": 中途被整点报时窗口截断（调用方应执行报时）
    - "stopped": 初始就没曲目 / 异常
    
    "播完"的检测顺序：
    - Music.app 自动切到了下一首 → 立刻 pause，ended_normally
    - Music.app 停止播放 → 算 ended_normally（也不需要 pause）
    - 锚定的剩余墙钟时间到了 → 立刻 pause，ended_normally
    - time_signal_checker.should_arm() 返回 plan → 立刻 pause，interrupted_by_time_signal
    
    pause 一定在这里做。
    """
    state = music.get_playback_state()
    if state.current_track is None:
        return "stopped"
    
    # 锚定到墙钟
    duration = state.current_track.duration
    position = state.position
    remaining_initial = duration - position
    pause_wall_time = time.monotonic() + (remaining_initial - pause_early_by)
    
    print(f"[runtime] 等《{track.name}》播完，时长 {duration:.0f}s，剩余 {remaining_initial:.0f}s")
    last_tick = time.monotonic()
    last_signal_check = time.monotonic()
    
    while True:
        now = time.monotonic()
        time_left = pause_wall_time - now
        
        # 时间到了 → 立刻 pause
        if time_left <= 0:
            music.pause()
            return "ended_normally"
        
        # 整点报时检查：每 1 秒查一次（应付分钟级别精度足够）
        if time_signal_checker is not None and (now - last_signal_check) >= 1.0:
            plan = time_signal_checker.should_arm()
            if plan is not None:
                music.pause()
                print(f"[runtime] 整点报时窗口到，打断当前曲目（剩余 {time_left:.0f}s）")
                return "interrupted_by_time_signal"
            last_signal_check = now
        
        # 周期性 prebake 状态汇报
        if on_tick and (now - last_tick) >= 5.0:
            try:
                on_tick(time_left)
            except Exception:
                pass
            last_tick = now
        
        # 检查曲目状态：自动切走 / 停止都算"播完"，必须立刻 pause
        try:
            state = music.get_playback_state()
        except RuntimeError:
            time.sleep(0.5)
            continue
        if state.current_track is None:
            print("[runtime] 播放停止（视为正常完成）")
            return "ended_normally"
        if (state.current_track.name, state.current_track.artist) != (track.name, track.artist):
            music.pause()
            print(f"[runtime] Music.app 自动切到了下一首（{state.current_track}），已 pause")
            return "ended_normally"
        
        # 自适应轮询间隔
        if time_left > 5.0:
            sleep_for = 3.0
        elif time_left > 1.0:
            sleep_for = 0.5
        else:
            sleep_for = 0.05
        sleep_for = min(sleep_for, time_left)
        time.sleep(sleep_for)


def get_next_song_preview(music: AppleMusicController) -> Optional[Track]:
    """拿播放列表里当前歌的下一首。随机播放模式下这会拿到错的下一首（已在启动时检测过）。"""
    try:
        return music.get_upcoming_track()
    except Exception as e:
        print(f"[runtime] 取下一首失败：{e}")
        return None


# ==================== 主流程 ====================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-segments", type=int, default=100,
                        help="最多跑几段就退出（station_id / song / host_talk 每个算一段）")
    parser.add_argument("--allow-shuffle", action="store_true",
                        help="即使 Music.app 开了随机播放也继续运行（不推荐）")
    args = parser.parse_args()
    
    # === 加载配置 ===
    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    
    print(f"[runtime] LLM provider: {cfg['llm']['provider']}")
    print(f"[runtime] TTS provider: {cfg['tts']['provider']}")
    
    # === 构造组件 ===
    generator, tts, music, host, time_signal = build_components(cfg)
    # （build_components 内部已经打印过主持人信息）
    
    # === 同步检查：这些不能等待，失败就退出，让用户解决 ===
    if not music.is_music_running():
        print("[runtime] Music.app 没有运行，请先打开并选好播放列表")
        sys.exit(1)
    
    if music.get_shuffle_enabled() and not args.allow_shuffle:
        print()
        print("=" * 60)
        print("[runtime] ⚠ Apple Music 当前开启了随机播放。")
        print("    由于 AppleScript 接口不暴露 shuffle 后的实际播放顺序，")
        print("    程序无法知道'下一首'是什么，DJ 介绍会出错。")
        print()
        print("    选择：")
        print("    1. 在 Music.app 里手动关闭随机播放，重新运行本程序")
        print("    2. 让程序帮你关掉随机播放（推荐）")
        print("    3. 强制继续（DJ 介绍的'下一首'会不准）：")
        print("       python demo_llm_runtime.py --allow-shuffle")
        print("=" * 60)
        choice = input("\n输入 2 让程序关掉，回车或其他键退出：").strip()
        if choice == "2":
            music.set_shuffle_enabled(False)
            print("[runtime] ✓ 已关闭随机播放")
        else:
            sys.exit(0)
    
    state = music.get_playback_state()
    if state.current_track is None:
        print("[runtime] 当前没有曲目，请在 Music.app 里选好播放列表/专辑")
        sys.exit(1)
    
    # 暂停 Music.app（如果在播），主循环从 station_id 开场开始
    if state.is_playing:
        music.pause()
        time.sleep(0.1)
    
    # === 可等待的检查：TTS / LLM 健康检查 + warmup ===
    # 这些步骤可能耗时较久（本地模型加载几十秒），所以在背景线程里跑，
    # 前台用 WaitingState 循环播 coming_soon + radio_promo 当过场带。
    warmup_state = {
        "done": False,
        "error": None,
        "tts_ok": False,
        "llm_ok": False,
    }
    
    def warmup_worker():
        try:
            # TTS 健康检查
            ok, msg = tts.health_check()
            warmup_state["tts_msg"] = msg
            if not ok:
                warmup_state["error"] = f"TTS 不可用: {msg}"
                return
            warmup_state["tts_ok"] = True
            
            # TTS warmup（首次加载参考音频）
            while not tts.is_warmup_done():
                time.sleep(1)
            
            # LLM 健康检查
            ok, msg = generator.health_check()
            warmup_state["llm_msg"] = msg
            if not ok:
                warmup_state["error"] = (
                    f"LLM 不可用: {msg}（检查 Ollama 是否运行、模型是否 pull）"
                )
                return
            warmup_state["llm_ok"] = True
            
            # LLM warmup（首次加载权重 10-30s）
            generator.warmup()
        except Exception as e:
            warmup_state["error"] = str(e)
        finally:
            warmup_state["done"] = True
    
    print("[runtime] 启动背景 warmup（TTS + LLM）...")
    worker = threading.Thread(target=warmup_worker, daemon=True)
    worker.start()
    
    # 用 WaitingState 当过场带
    try:
        ws = WaitingState.from_directory(STATION_ID_DIR)
        ws.run_until_ready(
            readiness_check=lambda: warmup_state["done"],
            max_wait_seconds=600,
            always_play_back=True,
        )
    except ValueError as e:
        # 找不到过场带文件，降级为静默轮询
        print(f"[runtime] WaitingState 不可用（{e}），静默等待 warmup...")
        for _ in range(300):
            if warmup_state["done"]:
                break
            time.sleep(1)
    
    if warmup_state.get("error"):
        print(f"[runtime] ✗ Warmup 失败: {warmup_state['error']}")
        sys.exit(1)
    
    if warmup_state.get("tts_msg"):
        print(f"[runtime] ✓ TTS: {warmup_state['tts_msg']}")
    if warmup_state.get("llm_msg"):
        print(f"[runtime] ✓ {warmup_state['llm_msg']}")
    print("[runtime] ✓ 所有 warmup 完成，准备进入主循环")
    
    # === 准备 Scheduler ===
    ua = UnannouncedSongs()
    scheduler = Scheduler(
        unannounced=ua,
        max_consecutive_songs=2,
        top_hour_guard_seconds=120,
        expected_host_talk_duration_seconds=30,
    )
    
    # === 运行主循环 ===
    print(f"\n[runtime] 开始电台主循环，最多 {args.max_segments} 段")
    print("=" * 60)
    
    # 维持一个"刚开始播的歌"对应的预生成任务的句柄
    pending_prebake: Optional[PrebakedScript] = None
    # 上一段 prebake 实际告诉 LLM "要引出"的歌——host_talk 段结束时用它通知 scheduler。
    # 不能用 decision.next_song_to_introduce，因为那是 host_talk 段开头主循环重新查的，
    # 此时 Apple Music 可能已经切走，导致取到的"下一首"和 prebake 引出的不是同一首。
    pending_introduced_next: Optional[Track] = None
    # 记录上一首歌的 Track，用于 song 段开头判断是否需要 next_track
    last_played_track: Optional[Track] = None
    
    segment_count = 0
    first_song = True
    
    # ============== 报时流程辅助 ==============
    def run_time_signal(plan):
        """执行报时序列。包括清理预生成 + scheduler 状态切到 after_time_signal。"""
        nonlocal pending_prebake, pending_introduced_next, last_played_track
        # 清理半成品的 prebake——歌被打断后那段 host_talk 不再合适
        if pending_prebake is not None:
            print("[runtime] 报时启动前清理未完成的 prebake")
            pending_prebake.cleanup()
            pending_prebake = None
            pending_introduced_next = None
        # 执行
        try:
            time_signal.execute(plan, before_signal=lambda: music.pause())
        except Exception as e:
            print(f"[runtime] 报时执行异常（继续走流程）: {e}")
        # 通知 scheduler："我们刚刚做了报时"——它会把 _last_kind 重置为 None，
        # 下一次决策返回 song（接歌），中间不会再插 station_id/host_talk。
        scheduler.note_time_signal_done()
        # 报时之后接 back_*（参考 demo_scripted.py 的"DJ 回来"过渡音）
        back_path = pick_station_id_file(["back_1", "back_2"])
        if back_path:
            afplay(back_path)
        # 注意：last_played_track 保留——下一个 song 段开头判断时会用，
        # 因为 Apple Music 还停在那首歌（被打断的或刚结束的）的某个位置。
    
    try:
        while segment_count < args.max_segments:
            # === 整点报时优先：在每段决策之前先查 ===
            if time_signal is not None:
                plan = time_signal.should_arm()
                if plan is not None:
                    segment_count += 1
                    print(f"\n--- 段 #{segment_count}（整点报时） ---")
                    run_time_signal(plan)
                    continue
            
            # 取下一首预览（决策时给 scheduler 用，host_talk 才知道引出谁）
            next_preview = get_next_song_preview(music)
            
            decision = scheduler.decide_next(next_song_preview=next_preview)
            segment_count += 1
            
            print(f"\n--- 段 #{segment_count} ---")
            print(f"[runtime] 决策: {decision.kind}（{decision.reason}）")
            print(f"[runtime] state: {scheduler.state_summary()}")
            
            if decision.kind == "station_id":
                # === STATION_ID ===
                if decision.situation == "opening":
                    path = pick_station_id_file(OPENING_STATION_ID_PATTERNS)
                else:
                    path = pick_station_id_file(MID_STATION_ID_PATTERNS)
                if path is None:
                    print("[runtime] 找不到 station_id 文件，跳过这段")
                else:
                    afplay(path)
                scheduler.note_station_id_done()
            
            elif decision.kind == "host_talk":
                # === HOST_TALK ===
                # 这里阻塞等待之前歌曲开始时启动的 prebake 结果
                if pending_prebake is None:
                    # 没有预生成？只能现场生成 + 合成（罕见——表示上一段不是歌）
                    print("[runtime] ⚠ 没有预生成的 host_talk，临时同步生成")
                    pending_prebake = PrebakedScript(
                        generator=generator,
                        tts=tts,
                        unannounced=decision.songs_to_announce,
                        next_song=decision.next_song_to_introduce,
                        situation=decision.situation,
                    )
                    pending_prebake.start()
                    pending_introduced_next = decision.next_song_to_introduce
                
                # 提前检查失败（避免起完垫音后才发现）
                if pending_prebake.is_done() and not pending_prebake.is_ready():
                    print(f"[runtime] ✗ 预生成失败: {pending_prebake.error}")
                    print("[runtime] 跳过这段 host_talk")
                    pending_prebake.cleanup()
                    pending_prebake = None
                    pending_introduced_next = None
                    scheduler.note_host_talk_done(announced_tracks=[], introduced_next=None)
                    continue
                
                if pending_prebake.is_ready():
                    print("[runtime] ✓ host_talk 已就绪（歌播完前已预生成），起垫音直接播")
                else:
                    print("[runtime] ⏳ host_talk 仍在合成，起垫音掩盖等待...")
                
                # 无论预生成是否已经 ready，host_talk 整段都要垫音陪衬——
                # 这是电台口播的核心听感。
                result = None
                with play_bed_music(volume=BED_MUSIC_VOLUME) as bed:
                    try:
                        result = pending_prebake.wait_and_get(timeout=PREBAKE_WAIT_TIMEOUT)
                    except Exception as e:
                        print(f"[runtime] 等待预生成失败: {e}")
                        if bed is not None:
                            bed.fade_out(BED_MUSIC_FADE_OUT)
                        pending_prebake.cleanup()
                        pending_prebake = None
                        pending_introduced_next = None
                        scheduler.note_host_talk_done(announced_tracks=[], introduced_next=None)
                        continue
                    
                    # 垫音持续，叠加播主持人台词
                    afplay(result.wav_path)
                    
                    # 关键无缝衔接：参考 demo_scripted.py——
                    # 台词刚播完、垫音还在响时，立刻把 Apple Music 推到下一首并 play。
                    # 这样下一首歌的开头是在垫音覆盖下进入的，听众听到的是
                    # "DJ 收尾 → 垫音继续 + 新歌淡入 → 垫音淡出"，没有任何空档。
                    # （Scheduler 的规则保证 host_talk 之后必接 song，所以这里直接切歌是安全的）
                    try:
                        cur_state = music.get_playback_state()
                        if cur_state.current_track is not None and last_played_track is not None:
                            cur = cur_state.current_track
                            if (cur.name, cur.artist) == (last_played_track.name, last_played_track.artist):
                                music.next_track()
                                time.sleep(0.1)
                        # 首次（first_song）情况下不在 host_talk 后处理，留给 song 段
                        if not first_song:
                            music.play()
                    except Exception as e:
                        print(f"[runtime] host_talk 末尾起下一首失败（song 段会兜底）: {e}")
                    
                    # 现在下一首已经在垫音掩盖下开始播了——淡出垫音
                    if bed is not None:
                        bed.fade_out(BED_MUSIC_FADE_OUT)
                
                pending_prebake.cleanup()
                
                # 通知 scheduler 这段 host_talk 介绍了哪些歌、引出了哪首。
                scheduler.note_host_talk_done(
                    announced_tracks=result.announced_tracks,
                    introduced_next=pending_introduced_next,
                )
                pending_prebake = None
                pending_introduced_next = None
            
            elif decision.kind == "song":
                # === SONG ===
                # 让 Apple Music 继续播（如果是开场后第一首，需要 play；之后是 next_track）
                # === 切到正确的曲目并起播 ===
                # 时序：station_id / host_talk 段期间 Apple Music 一直 paused 在上一首尾部
                # （pause 失败的话可能已经自动续到下一首）。
                state = music.get_playback_state()
                if state.current_track is None:
                    print("[runtime] Music.app 没有曲目了，退出")
                    break
                
                if first_song:
                    try:
                        music.set_position(0)
                    except Exception as e:
                        print(f"[runtime] 回到曲目开头失败（继续）: {e}")
                    first_song = False
                    music.play()
                    time.sleep(0.15)
                else:
                    # 判断是否需要 next_track：
                    # - 当前曲目还是上一首歌 → 上一段没把切歌做完（典型：上一段是 station_id），这里补上
                    # - 当前曲目已经是新歌 → 上一段（host_talk）已经在垫音掩盖下切好了，不要再次 next
                    cur = state.current_track
                    needs_switch = (
                        last_played_track is not None
                        and (cur.name, cur.artist) == (last_played_track.name, last_played_track.artist)
                    )
                    if needs_switch:
                        music.next_track()
                        time.sleep(0.1)
                        music.play()
                        time.sleep(0.15)
                    else:
                        # 上一段已经切好歌并 play 了（host_talk 末尾做的），这里只确认状态
                        if not state.is_playing:
                            music.play()
                            time.sleep(0.1)
                        else:
                            print(f"[runtime] Apple Music 已在播 {cur}，无需再切")
                
                state = music.get_playback_state()
                if state.current_track is None:
                    print("[runtime] 起播失败，退出")
                    break
                
                current = state.current_track
                print(f"[runtime] 正在播: {current}")
                
                # 这首歌结束后是 host_talk？是的话，立刻后台启动预生成
                predicted_next_break = scheduler.predict_next_break_after_song()
                if predicted_next_break == "host_talk":
                    # 预生成需要知道再下一首是谁（用于"引出"）；debug=True 让 AppleScript
                    # 拿不到下一首时把原因打出来——便于排查 LLM 没引出下一首的问题
                    upcoming = music.get_upcoming_track(debug=True)
                    if upcoming is None:
                        print("[runtime] ⚠ 拿不到下一首信息——host_talk 将不会引出下一首。")
                        print("           可能原因：当前用的是 Apple Music 推荐电台/Radio，")
                        print("           或者当前曲目不在某个具体播放列表里。")
                        print("           建议：用一个明确的播放列表，关掉随机播放。")
                    # 这次 host_talk 要介绍的歌 = 当前 unannounced + 这首正要播的（因为它将来也要被介绍）
                    will_announce = scheduler.unannounced.all() + [current]
                    # 估算 host_talk 真正开播时刻：约等于 current.duration - state.position 秒后
                    # （加 1 秒缓冲：垫音启动 + afplay 加载）
                    play_offset = max(0.0, current.duration - state.position) + 1.0
                    print(f"[runtime] >>> 启动后台预生成（介绍 {len(will_announce)} 首 + 引出 {upcoming}，"
                          f"台词里的时间将以 +{play_offset:.0f}s 后计算）")
                    pending_prebake = PrebakedScript(
                        generator=generator,
                        tts=tts,
                        unannounced=will_announce,
                        next_song=upcoming,
                        situation="between_songs",
                        time_offset_seconds=play_offset,
                    )
                    pending_prebake.start()
                    pending_introduced_next = upcoming  # 记下来，host_talk 段尾要用
                else:
                    print("[runtime] 下一段是 station_id，无需预生成")
                
                # 等歌播完
                def _tick(time_left):
                    if pending_prebake is None:
                        return
                    if pending_prebake.is_ready():
                        status = "✓ 已就绪"
                    elif pending_prebake.is_done():
                        status = f"✗ 失败: {pending_prebake.error}"
                    else:
                        status = "⏳ 合成中"
                    print(f"         [prebake {status}] 歌剩 {time_left:.0f}s")
                
                wait_result = wait_for_song_to_end(
                    music, current,
                    on_tick=_tick,
                    time_signal_checker=time_signal,
                )
                # 注意：wait_for_song_to_end 已经在内部 pause 了，这里不要再 pause/sleep——
                # 任何额外延迟都会变成"段间空档"
                
                # 处理"这首歌是不是被上轮 host_talk 引出过"
                was_announced = scheduler.consume_pending_announced_next(current)
                
                if wait_result == "interrupted_by_time_signal":
                    # 歌被报时打断——按规则计入"播过但没介绍完"
                    scheduler.note_song_finished(
                        current,
                        was_announced_before=was_announced,
                        was_interrupted=True,
                    )
                    last_played_track = current
                    # 立刻执行报时（用最新 plan 而不是 wait_for_song_to_end 里的旧 plan）
                    plan = time_signal.should_arm() if time_signal else None
                    if plan is None:
                        # 极小概率：从触发到这里已经过了窗口。强制再 arm 一次或跳过
                        print("[runtime] ⚠ 报时 plan 已过期，跳过这次报时")
                    else:
                        run_time_signal(plan)
                    continue
                
                # 正常播完
                scheduler.note_song_finished(current, was_announced_before=was_announced)
                
                if wait_result == "stopped":
                    print("[runtime] 歌曲意外结束，退出")
                    break
                
                last_played_track = current
            
            else:
                print(f"[runtime] 未知决策类型: {decision.kind}")
                break
        
        print(f"\n[runtime] 跑完 {segment_count} 段，正常退出")
    
    except KeyboardInterrupt:
        print("\n[runtime] 收到中断，停止")
    finally:
        if pending_prebake is not None:
            pending_prebake.cleanup()
        try:
            music.pause()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except FileNotFoundError as e:
        print(f"[runtime] 错误: {e}")
        sys.exit(1)
    except RuntimeError as e:
        print(f"[runtime] 错误: {e}")
        sys.exit(1)