"""
IndexTTS-2 HTTP API 服务（给 HITFM 项目用）

放在 index-tts 项目根目录（和 webui.py 同级）。

启动:
    # 基础启动
    uv run python api_server.py
    
    # 启动时自动 warmup（推荐）—— 用配置好的参考音频先做一次特征提取，
    # 这样后续合成不会卡在"第一次见到这个参考音频"的开销上
    uv run python api_server.py --warmup-ref-audio /path/to/dj.wav

端点:
    GET  /health   返回 {status, model_loaded, warmup_done}
    POST /tts      合成语音，返回 wav 字节流

Warmup 是后台异步进行的：
    服务启动后立刻返回 ready，但 warmup_done 字段会在后台 warmup 完成后翻 true。
    HITFM 客户端通过轮询 /health 等待 warmup_done。
"""

import os
import sys
import argparse
import tempfile
import threading
import time
import traceback
from flask import Flask, request, send_file, jsonify

from indextts.infer_v2 import IndexTTS2


# 全局状态
tts: IndexTTS2 = None
warmup_done = False  # 是否已对配置的参考音频做过特征预热
warmup_ref_audio: str = ""  # 当前已暖机的参考音频路径
_inference_lock = threading.Lock()  # 模型推理串行锁（IndexTTS-2 单实例不能并发推理）

app = Flask(__name__)


def _do_warmup(ref_audio: str):
    """后台线程：用 ref_audio 跑一次最小合成，让模型提取并缓存说话人特征"""
    global warmup_done, warmup_ref_audio
    
    if not os.path.exists(ref_audio):
        print(f"[warmup] 警告：参考音频不存在 {ref_audio}，跳过 warmup")
        return
    
    print(f"[warmup] 开始预热: {ref_audio}")
    t0 = time.time()
    
    fd, tmp_out = tempfile.mkstemp(suffix=".wav", prefix="warmup_")
    os.close(fd)
    
    try:
        with _inference_lock:
            tts.infer(
                spk_audio_prompt=ref_audio,
                text="预热。",  # 极短文本
                output_path=tmp_out,
                verbose=False,
            )
        warmup_ref_audio = ref_audio
        warmup_done = True
        print(f"[warmup] 完成，耗时 {time.time() - t0:.1f}s")
    except Exception as e:
        print(f"[warmup] 失败: {e}")
        traceback.print_exc()
    finally:
        if os.path.exists(tmp_out):
            try:
                os.unlink(tmp_out)
            except Exception:
                pass


@app.route("/health", methods=["GET"])
def health():
    """健康检查 + warmup 状态查询"""
    return jsonify({
        "status": "ok",
        "model_loaded": tts is not None,
        "warmup_done": warmup_done,
        "warmup_ref_audio": warmup_ref_audio,
    })


@app.route("/tts", methods=["POST"])
def synthesize():
    try:
        payload = request.get_json(force=True)
    except Exception as e:
        return jsonify({"error": f"无效的 JSON: {e}"}), 400
    
    text = payload.get("text")
    ref_audio_path = payload.get("ref_audio_path")
    
    if not text:
        return jsonify({"error": "缺少字段: text"}), 400
    if not ref_audio_path:
        return jsonify({"error": "缺少字段: ref_audio_path"}), 400
    if not os.path.exists(ref_audio_path):
        return jsonify({"error": f"参考音频文件不存在: {ref_audio_path}"}), 400
    
    emo_audio_prompt = payload.get("emo_audio_prompt")
    emo_alpha = float(payload.get("emo_alpha", 1.0))
    emo_vector = payload.get("emo_vector")
    emo_text = payload.get("emo_text")
    use_emo_text = bool(payload.get("use_emo_text", False))
    use_random = bool(payload.get("use_random", False))
    verbose = bool(payload.get("verbose", False))
    max_text_tokens_per_segment = int(payload.get("max_text_tokens_per_segment", 120))
    
    fd, output_path = tempfile.mkstemp(suffix=".wav", prefix="indextts_")
    os.close(fd)
    
    try:
        # 用全局锁串行化推理——IndexTTS-2 单实例不支持并发
        # 但 Flask 服务本身可以并发接受请求（threaded=True），
        # 这样 /health 等轻量端点不会被推理阻塞
        with _inference_lock:
            tts.infer(
                spk_audio_prompt=ref_audio_path,
                text=text,
                output_path=output_path,
                emo_audio_prompt=emo_audio_prompt,
                emo_alpha=emo_alpha,
                emo_vector=emo_vector,
                use_emo_text=use_emo_text,
                emo_text=emo_text,
                use_random=use_random,
                verbose=verbose,
                max_text_tokens_per_segment=max_text_tokens_per_segment,
            )
        
        return send_file(
            output_path,
            mimetype="audio/wav",
            as_attachment=False,
            download_name="speech.wav",
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"合成失败: {str(e)}"}), 500


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9881)
    parser.add_argument("--model-dir", type=str, default="checkpoints")
    parser.add_argument("--fp16", action="store_true")
    parser.add_argument("--deepspeed", action="store_true")
    parser.add_argument("--cuda-kernel", action="store_true")
    parser.add_argument("--warmup-ref-audio", type=str, default="",
                        help="启动后用这个参考音频做一次预热合成")
    args = parser.parse_args()
    
    global tts
    print(f"[api_server] 加载模型 from {args.model_dir} ...")
    tts = IndexTTS2(
        model_dir=args.model_dir,
        cfg_path=os.path.join(args.model_dir, "config.yaml"),
        use_fp16=args.fp16,
        use_deepspeed=args.deepspeed,
        use_cuda_kernel=args.cuda_kernel,
    )
    print(f"[api_server] 模型加载完成")
    
    if args.warmup_ref_audio:
        # 后台线程做 warmup，不阻塞 server 启动
        t = threading.Thread(
            target=_do_warmup,
            args=(args.warmup_ref_audio,),
            daemon=True,
        )
        t.start()
        print(f"[api_server] 后台 warmup 已启动，client 可通过 /health 查询 warmup_done 字段")
    else:
        # 没指定 warmup 音频——直接标 done，避免 client 一直等
        global warmup_done
        warmup_done = True
        print("[api_server] 未指定 --warmup-ref-audio，跳过 warmup")
    
    print(f"[api_server] 监听 http://{args.host}:{args.port}")
    
    # threaded=True：Flask 接受并发请求
    # 推理本身被 _inference_lock 串行化，但 /health 等不会被推理阻塞
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()