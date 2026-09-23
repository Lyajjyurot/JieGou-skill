# 解构skill · video-to-director-script

把一个视频解构成**导演级分镜脚本**。

程序先把视频切成镜头、抽出关键帧、算出客观指标，再把画面摊成可逐帧阅读的接触表；
模型读图后判断画面内容、人物动作、情绪与质感，最后按你给的模板写出带完整技术参数的
分镜脚本。

**核心主张：程序负责测量，模型负责解读。** 色号、色温、明暗比、颗粒、响度、BPM、卡点率
这些数字来自代码实测，不靠模型猜；画面里"发生了什么、什么情绪、像谁拍的"才是模型的工作。

---

## 它解决什么问题

多模态模型直接"看视频"有三个硬伤：**抽帧稀疏导致漏看点、没有时间轴导致对不上号、
技术参数全靠猜导致色彩光影描述不可用**。

本项目把这三件事拆开：镜切与抽帧交给算法（可复现），时间码烧进画面（可对齐），
技术参数交给测量（可验证）。模型只做它真正擅长的事。

具体做法：

| 环节 | 谁做 | 产出 |
|---|---|---|
| 镜头切分 | 算法 | 直方图 + 缩略图差异，中位数/MAD 自适应阈值 |
| 关键帧抽取 | 算法 | 按时长自适应预算，镜内去重但有下限 |
| 运镜分析 | 算法 | 稠密光流分解为位移/推拉/主体动作三路 |
| 影像指标 | 算法 | 调色板 hex、色温、明暗比、暗部占比、颗粒、锐度、景深比 |
| 音频分析 | 算法 | LUFS、频段画像、人声/音乐/静音分段、BPM、卡点率、静默区间 |
| 画面语义 | 模型 | 内容、动作、视线、表演、情绪、质感、风格参照 |
| 脚本成文 | 模型 | 按模板输出逐镜字段 |

---

## 快速开始

### 1. 安装依赖

```bash
python -m pip install -r requirements.txt
```

不需要单独安装 ffmpeg —— `imageio-ffmpeg` 自带二进制。

> 国内网络：部分镜像源（如清华源）缺 `opencv-python-headless` 与 `imageio-ffmpeg`，
> 会报 `No matching distribution found`。换源即可：
> `-i https://mirrors.aliyun.com/pypi/simple/`

### 2. 验证环境

```bash
python scripts/run_pipeline.py --selftest
```

会现场合成一段三镜测试视频并跑完整流程，打印校验结果。通过即环境可用。

### 3. 解构一个视频

```bash
python scripts/run_pipeline.py /path/to/video.mp4 --out ./out
```

产物：

```
out/
├── context_pack.md      ★ 实测数据汇总，模型的主要事实来源
├── meta.json            素材元数据
├── shots.json           镜头切分 + 关键帧索引 + 光流运镜指标
├── shots_metrics.json   逐镜客观指标
├── audio.json           响度 / 分段 / 节拍 / 卡点 / 静默
├── frames/              关键帧 JPEG（文件名含镜号与毫秒时间码）
└── sheets/
    ├── overview_*.jpg   全局接触表，20 格/张，烧入时间码
    └── detail_sXX.jpg   逐镜接触表，≤9 格，烧入时间码
```

### 4. 让模型写脚本

把 `context_pack.md` 和 `sheets/*.jpg` 交给支持视觉的模型，配合 `SKILL.md` 的工作流即可。
若使用 WorkBuddy 等支持 Agent Skill 的工具，安装后直接说"解构这个视频"就会自动触发。

```bash
python scripts/build_skill.py --install                # 装到 ~/.workbuddy/skills/
python scripts/build_skill.py --install --dest <dir>   # 装到指定技能目录
python scripts/build_skill.py --check                  # 看会打包哪些文件
```

---

## 输出长什么样

脚本按模板输出，每镜包含 14 个字段。字段值有实测依据，例如：

```
【镜头3：指尖推开抽屉】| 0:04-0:06 | 视角=林晚 | Beat 2-发现
[机位] 林晚主观侧后方 · 平视略俯 · 约与桌面同高 · 近景切特写
[镜头] 等效 50mm · f/1.8 · 极浅景深，仅指尖与抽屉缝在焦内
[运镜] 手持跟拍 · 缓慢 · 幅度约 5cm 微推（实测位移 0.04 帧宽/秒）
[光影] 主光 3200K 暖钨丝，来自左上 · 暗部占比 41% · 对比度 0.58 ·
       画面明暗比约 3.5 级
[色号] dominant #3E2E24 / accent #C9A227 / shadow #14100D / unique #8FA9B8
[材质] 皮肤保留毛孔细节；胡桃木纹可辨，表面哑光；颗粒感 σ1.9（轻胶片）
[声音] 环境底噪约 -42dBFS；木质摩擦声 0.3s；对白后静默 0.4s 留白
[对白] 林晚（中低音区/气息略紧/语速偏慢/克制）：……你自己看。
       （起 0:04.6，止 0:05.4，波形判定为人声主导段）
[本镜NOT] NOT 多余手指 · NOT 美甲装饰 · NOT 抽屉内物件清晰可见
[Style Lock] 参考《燃烧女子的肖像》的手部特写调度与自然光逻辑……核心张力是
       「接触前的迟疑」——动作已发生，意愿仍在犹豫。
```

> 上例用于说明字段颗粒度。完整字段规范见 `references/script_template.md`。

---

## 测量能力

### 影像（逐镜 + 全片聚合）

| 指标 | 说明 |
|---|---|
| 调色板 | dominant / accent / shadow / unique 四角色真实 hex（k-means） |
| 亮度分布 | 五数概括、暗部占比、高光占比 |
| 对比度 | p95−p5 归一 |
| 画面明暗比 | 亮部 15% / 暗部 15% 亮度比，对数级数（分母设下限避免爆炸） |
| 色温 | 估计色温 K + 暖/中性/冷判定 |
| 饱和度 | 均值、高饱和像素比、单色判定 |
| 颗粒 | 高频残差 σ，映射胶片/数码质感 |
| 锐度 | 归一化拉普拉斯方差 |
| 景深线索 | 中心/边缘清晰度比（**弱线索，需读图确认**） |
| 分离调色 | 暗部色相 vs 亮部色相，识别青橙调等 |
| 肤色占比 | 人物存在线索（**会误报，需读图确认**） |
| 画幅 | 上下黑边检测 |

### 运镜（稠密光流分解）

| 指标 | 单位 | 含义 |
|---|---|---|
| `cam_move` | 帧宽/秒 | 全局平移速度（摇 / 移 / 跟） |
| `cam_dx` `cam_dy` | 帧宽/秒 | 水平 / 垂直分量，定方向 |
| `cam_zoom` | /秒 | 散度，正=推近，负=拉远 |
| `local_motion` | 帧宽/秒 | P90 残差流速 = 主体动作强度 |

组合读法可以区分**「摄影机在动」还是「人在动」**：

- `cam_move` 高 + `local_motion` 低 → 镜头在动，画面内容静止
- `cam_move` 低 + `local_motion` 高 → 机位固定，主体在动
- 两者都高 → 跟拍
- 两者都低 → 静态构图

### 音频（纯波形分析，无需语音识别）

| 指标 | 说明 |
|---|---|
| 响度 | 积分 LUFS（解析 ebur128 Summary）、LRA、真峰值 |
| 频段画像 | sub / bass / low-mid / mid / high-mid / high 六段能量占比 |
| 分段 | 静音留白 / 疑似人声主导 / 疑似音乐主导 / 环境氛围 / 混合（含置信度） |
| 节拍 | 谱通量 onset 包络 + 自相关测 BPM |
| **卡点率** | 切换点与节拍网格的对齐度——量化"剪辑是否踩点" |
| 静默 | silencedetect 区间，用于留白分析 |

音频分类是启发式判定，**一律标注"疑似"**。本项目不含语音识别，
`[对白]` 字段不会编造台词：有字幕轨就抽字幕，没有就只写"有对白 + 起止时间码 + 波形推测的语气语速"。

---

## 目录结构

```
.
├── SKILL.md                          Skill 定义与工作流（含 AI 执行指令）
├── scripts/
│   ├── run_pipeline.py               一条命令跑完整流水线（含 --selftest）
│   ├── probe_video.py                元数据探测
│   ├── extract_keyframes.py          镜头切分 + 光流 + 关键帧 + 接触表
│   ├── analyze_shots.py              逐镜影像指标
│   ├── analyze_audio.py              音频波形分析
│   ├── build_context.py              汇总为 context_pack.md
│   ├── build_skill.py                打包 / 安装为标准 Skill 目录
│   └── vds_common.py                 公共工具（ffmpeg 定位、Unicode 安全图像读写等）
├── references/
│   ├── script_template.md            字段规范、硬性要求、数据来源、自检清单
│   ├── analysis_rules.md             实测数字 → 导演术语映射（含情绪四步判定法）
│   ├── style_lock_library.md         导演风格指纹卡、核心张力原型、情绪弧线结构
│   └── output_template.md            输出模板骨架（用户自备，不随仓库分发）
├── requirements.txt
└── LICENSE
```

流水线：

```
video
  ↓ probe_video.py        元数据 / 音轨 / 字幕轨 / 时长档位
  ↓ extract_keyframes.py  镜切 → 光流 → 自适应抽帧 → 去重 → 接触表（烧时间码）
  ↓ analyze_shots.py      调色板 / 光影 / 材质 / 景深 逐镜指标
  ↓ analyze_audio.py      LUFS / 分段 / BPM / 卡点率 / 静默
  ↓ build_context.py      → context_pack.md
  ↓ [模型读图]
  → 导演级分镜脚本
```

---

## 关于输出模板

输出格式由模板决定，**模板不随本仓库分发**（版权归属可能不属于本项目）。

优先级：

1. `references/output_template.md` —— 你自己的模板放这里
2. 对话中直接给出的模板
3. 都没有时，用 `references/script_template.md` 的**字段清单**组装通用结构

字段的含义、硬性要求、数据来源、降级写法、自检清单在 `references/script_template.md`，
无论用哪份模板都适用。

---

## 已知限制

诚实列出，避免误用：

- **音频分类是启发式的**，不含语音识别。要精确台词需自备字幕或接入 ASR。
- **`light_ratio_stops` 是画面级明暗比，不是布光光比。** 布光光比是主体上的照度比，
  无法从成片测出。
- **`fg_bg_sharpness_ratio` 只是景深的弱线索。** 背景为平滑面（天空/纯色墙）时会虚高，
  必须读图确认再写光圈。
- **`skin_ratio` 会误报**，暖米色、食物、木质、黄昏天空都会触发。
- **长视频（>3min）是抽样解构**，会漏看细节。建议先出粗版确认结构，再对重点段落重跑。
- **光流对高速运动与低纹理画面会低估**。极端情况下需完全依赖读图判断运镜。
- **`motion` 字段不要用于判断运镜**（它是直方图差异，只作切点强度参考）。

---

## 许可与第三方组件

本项目代码以 **MIT License** 发布，详见 [LICENSE](LICENSE)。

**第三方依赖许可：**

| 依赖 | 许可 |
|---|---|
| numpy | BSD-3-Clause |
| opencv-python-headless | Apache-2.0 |
| Pillow | MIT-CMU |
| imageio-ffmpeg | BSD-2-Clause |

**⚠️ 关于 FFmpeg：** 本项目在运行时会通过 `imageio-ffmpeg` 使用 **FFmpeg**。
`imageio-ffmpeg` 自身是 BSD-2-Clause，但其分发的 FFmpeg 二进制构建通常为 **GPL** 许可。
FFmpeg 是独立程序，其许可独立于本项目；但**若你把本项目与 FFmpeg 二进制一起打包分发，
需自行遵守 FFmpeg 的许可条款**（包含许可证全文、提供对应源码等）。我们未在本仓库中
附带任何 FFmpeg 二进制。

**关于生成内容：** 本工具仅在本机处理用户提供的视频，不联网、不上传任何数据。
使用者对自己处理的视频素材与生成的脚本负责；模板中出现的导演、影片、风格名称为
描述性参考，不代表与相关权利人的任何关联或授权。

---

## 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。改动流水线逻辑时请一并跑
`python scripts/run_pipeline.py --selftest` 确认全链路可用。

## 更新记录

见 [CHANGELOG.md](CHANGELOG.md)。
