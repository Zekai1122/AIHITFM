# HITFM Local

一个本地运行的 AI 电台。让 LLM 当主持人，介绍你 Apple Music 里正在播的歌；用 IndexTTS-2 克隆出你想要的主持人声音；按真实电台的节奏穿插过场白、整点报时、台呼。

> 灵感来自 [HITFM](https://baike.baidu.com/item/中央广播电视总台劲曲调频/58616285)（中国国际广播电台 Hit FM 音乐频道）。这是一个个人爱好项目，专为怀念 HITFM 的听友而制作，不隶属于任何电台机构，不用于任何商业目的。

## 它能做什么

- 监听 Apple Music 当前播放，让 LLM 在歌曲间隙生成口播文案
- 用 IndexTTS-2 克隆音色合成主持人的声音
- 在歌曲间隙穿插 **台呼**（station_id）和 **过场白**（host_talk），并自动交替
- 在垫音（bed music）下播口播，做到无缝衔接
- **整点报时**：在每个整点自动播报时间，pis 长嘀对齐到整点 00:00
- **多主持人**：在 `hosts/` 下放不同的文件夹即可切换
- **多时区**：自动识别系统时区，台词里的报时用当地时区表达（"悉尼时间"、"北京时间"…）
- 节目里所有的歌都会被介绍到（已介绍过的不会重复介绍）

## 系统要求

- **macOS**（依赖 AppleScript 控制 Apple Music。Linux/Windows 暂不支持）
- 至少 16 GB 内存（IndexTTS-2 + 7B LLM 同时跑）
- 推荐 Apple Silicon 芯片
- 大约 15 GB 磁盘空间（LLM 模型 ~5 GB，IndexTTS-2 ~7 GB，依赖包等）

## 快速开始

### 1. 安装

```bash
# 1. 克隆本仓库
git clone https://github.com/<your-name>/HITFM.git
cd HITFM

# 2. 一键安装（脚本会安装 brew 包、uv、ollama、IndexTTS、LLM 模型；默认LLM模型为 qwen2.5:7b-instruct）
./install.sh

# 国内用户：IndexTTS 模型权重改从 ModelScope 下载（比 HuggingFace 快很多）
./install.sh --indextts-mirror modelscope
 
# (可选) 指定别的 LLM 模型（可与上面的选项组合）：
./install.sh --model qwen2.5:14b                                  # 更大更聪明，需要更多内存
./install.sh --model llama3.2:3b                                  # 更小，适合低配机
./install.sh --model qwen2.5:14b --indextts-mirror modelscope     # 国内用户 + 大模型
```

安装大约需要 20-40 分钟，主要时间在下载 IndexTTS-2 模型权重（~7 GB）和 Ollama 模型（~5 GB）。国内用户强烈建议加 `--indextts-mirror modelscope`，否则 HuggingFace 下载速度可能极慢甚至失败。

### 2. 准备你的主持人

仓库里自带 `hosts/default/`（默认，男声）。如果想用自己的声音把 `hosts/default/voice_ref.wav` 替换成自己声音（注意保持文件名不变）。
或者：
```bash
mkdir hosts/myhost
# 准备 5-10 秒清晰的单人讲话录音作为参考音色
cp ~/Downloads/my_voice.wav hosts/myhost/voice_ref.wav
# 写主持人 profile
cat > hosts/myhost/profile.json <<EOF
{
  "name": "主持人的名字",
  "gender": "male"
}
EOF

# 然后编辑 `config.yaml` 把 `host:` 改成 `myhost`。
```

### 3. 准备 Apple Music

打开 Music.app，**关闭随机播放**，**选一个播放列表或专辑的一首歌播放然后暂停**，这首歌将是电台播放的第一首歌。

> Apple Music 的 AppleScript 接口不暴露 shuffle 后的真实顺序，所以随机播放下程序拿不到"下一首"信息。`start.sh` 启动时会检查并提示你关闭随机播放。

### 4. 启动

```bash
./start.sh
```

`start.sh` 会自动：

1. 确认 ollama 在跑（不在就启动）
2. 在后台启动 IndexTTS API server（带 warmup）
3. 等 server ready
4. 启动 HITFM 主程序

首次运行 macOS 会弹窗问是否允许 Python 控制 Music.app——**必须允许**。

Ctrl+C 退出时 `start.sh` 会清理 IndexTTS 后台进程。

## 配置

主配置在 `config.yaml`：

| 字段 | 说明 |
|---|---|
| `host` | 用哪个主持人（对应 `hosts/<host>/` 文件夹） |
| `llm.provider` | LLM 提供商（默认 `ollama`，本地） |
| `llm.ollama.model` | LLM 模型名（默认 `qwen2.5:7b-instruct`） |
| `time.time_zone_mode` | `auto` / `beijing` / IANA 名（如 `Australia/Sydney`） |
| `slogans` | 口播收尾会随机选一条 slogan |
| `tts.provider` | TTS 提供商（默认 `indextts`，可选 `say` 用 macOS 自带语音） |

## 项目结构

```
HITFM/
├── install.sh                 # 一键安装
├── start.sh                   # 一键启动
├── config.yaml                # 主配置
├── demo_llm_runtime.py        # 主程序入口
├── core/                      # 核心模块
│   ├── scheduler.py           # 调度状态机：决定下一段播什么
│   ├── music_controller.py    # Apple Music 控制（AppleScript）
│   ├── script_generator.py    # LLM 调用 + prompt 工程
│   ├── tts.py                 # TTS 抽象（say / IndexTTS HTTP）
│   ├── prebaked_script.py     # 后台预生成 LLM 台词 + TTS 合成
│   ├── bed_music.py           # 垫音播放器
│   ├── time_signal.py         # 整点报时
│   ├── broadcast_time.py      # 时区 / 季节
│   ├── host.py                # 主持人加载
│   └── waiting_state.py       # 启动等待时的电台过场
├── hosts/                     # 主持人目录
│   └── my_host/
│       ├── profile.json       # 元信息
│       └── voice_ref.wav      # TTS 参考音色
├── audio/                     # 电台音频素材
│   ├── NOTICE.md              # 音频素材版权说明
│   ├── station_id/            # 台呼 / coming_soon / radio_promo / back
│   ├── hours/                 # 整点报时（0.mp3-23.mp3 + pis.mp3）
│   ├── bed_music/             # 口播垫音
│   └── voice_refs/            # （旧）参考音色，已被 hosts/<host>/voice_ref 替代
├── prompts/
│   └── host_persona.md        # 主持人风格 prompt
└── external/
    ├── api_server.py          # IndexTTS HTTP API 包装（由 install.sh 复制进 IndexTTS）
    └── index-tts/             # 安装时 clone 的 IndexTTS 仓库
```

## 故障排查

### `start.sh` 启动后说"IndexTTS server 启动超时"

查日志找原因：

```bash
tail -100 .indextts_server.log
```

常见原因：
- IndexTTS 模型权重没下完整 → 重跑 `./install.sh`
- 内存不够 → 关闭其他占内存的程序
- Apple Silicon 上首次加载 MPS 后端慢，再多等 30-60 秒

### LLM 总是答非所问 / 不按格式输出

试试更大的模型：

```bash
ollama pull qwen2.5:14b
# 改 config.yaml: llm.ollama.model -> qwen2.5:14b
```

### Apple Music 切歌之间漏出下一首前奏

`demo_llm_runtime.py` 里的常量 `PAUSE_EARLY_BY` 控制提前 pause 多少秒（默认 0.5）。如果你听到漏音，可以加大到 1.0；如果听到歌尾被截掉，减小到 0.2。

### 主持人的中文里有北京口音的儿化音

这是 IndexTTS 复制参考音色的所有口音特征导致的。解决方案：换一段不带儿化音的参考音频放进 `hosts/<host>/voice_ref.wav`。

## 音频素材说明 / Audio Assets Notice

### 中文

`audio/` 目录下的电台台宣及飞标音频素材（包括台呼、报时、过场等）均来源于网络，版权归原权利人所有。素材来源参考：

- https://www.bilibili.com/video/BV1cF4m1L736/
- https://www.bilibili.com/video/BV14j411T7LF/

垫音（`audio/bed_music/`）来源于以下 YouTube 视频，版权归原作者所有：

- **SUMMER VIBES - Music Beds**，by Music Beds for Radio Imaging Professionals
  https://www.youtube.com/watch?v=IOtHymH99oU

**如果你是上述任意素材的版权方，并认为本项目的使用侵犯了你的权益，请通过 GitHub Issue 或邮件联系我，我将立即删除相关内容。**

本项目中的所有音频素材**仅供本项目内学习、演示使用**，禁止以任何形式单独提取、分发、传播或用于其他用途。

### English

The station ID, time signal, and transition audio assets in `audio/` were sourced from the internet. All rights belong to their respective copyright holders. Reference sources:

- https://www.bilibili.com/video/BV1cF4m1L736/
- https://www.bilibili.com/video/BV14j411T7LF/

The bed music (`audio/bed_music/`) was sourced from the following YouTube video. All rights belong to the original creator:

- **SUMMER VIBES - Music Beds**, by Music Beds for Radio Imaging Professionals
  https://www.youtube.com/watch?v=IOtHymH99oU

**If you are the copyright holder of any of these materials and believe their use in this project infringes your rights, please contact me via a GitHub Issue or email. I will remove the relevant content immediately upon request.**

All audio assets in this project are provided **solely for learning and demonstration purposes within this project**. Extracting, redistributing, rebroadcasting, or using them for any other purpose in any form is strictly prohibited.

---

## License

### 中文

本项目代码以 **Apache 2.0 + Commons Clause 1.0** 发布——见 [LICENSE](LICENSE)。

这意味着：

- ✅ 可以个人使用、学习、修改、分享
- ❌ **严禁任何形式的商业用途**（包括但不限于：付费服务、商业产品集成、广告变现等）

本项目是一个**纯个人爱好项目**，专为怀念 HITFM 的听友而制作。请不要将其用于任何商业目的。

此外，本项目依赖的 **IndexTTS-2 模型权重**本身即有独立的非商业许可证，独立禁止商业用途（详见 IndexTTS-2 的相关许可）。

完整的第三方组件归属信息见 [NOTICE](NOTICE)。

### English

This project's code is released under **Apache 2.0 + Commons Clause 1.0** — see [LICENSE](LICENSE).

This means:

- ✅ Personal use, learning, modification, and sharing are permitted
- ❌ **Any commercial use is strictly prohibited** (including but not limited to: paid services, integration into commercial products, monetization through advertising, etc.)

This is a **personal hobby project** made for fans who miss HITFM. Please do not use it for any commercial purpose.

Additionally, the **IndexTTS-2 model weights** used by this project carry their own independent non-commercial license, independently prohibiting commercial use (see IndexTTS-2's license for details).

For full third-party component attribution, see [NOTICE](NOTICE).

## 致谢

- [IndexTTS-2](https://github.com/index-tts/index-tts) by Bilibili — 主持人声音的核心
- [Ollama](https://github.com/ollama/ollama) — 本地 LLM 运行环境
- [Qwen2.5](https://huggingface.co/Qwen) by 阿里通义 — 默认 LLM
- HITFM 真实节目 — 节目结构、口播风格的灵感来源
