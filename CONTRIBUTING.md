# 贡献指南

感谢参与。这个项目同时是「工具」和「Agent Skill」，改动时请兼顾两端。

## 环境准备

```bash
python -m pip install -r requirements.txt
python scripts/run_pipeline.py --selftest     # 必须通过
```

`--selftest` 会现场合成测试视频并跑完整流程，**任何改动流水线逻辑的提交都应先让它通过**。

## 项目结构约定

- `scripts/` —— 可独立运行的 Python 脚本，彼此通过命令行调用
- `references/` —— 供模型加载的知识文档（Markdown），不参与运行
- `SKILL.md` —— Skill 定义与 AI 工作流指令，`description` 决定技能何时被触发

`scripts/` 里的每个脚本都应能**单独执行**（有 `argparse` 入口 + `if __name__ == "__main__"`），
不要引入只有流水线整体运行时才成立的隐式状态。

## 代码规范

- Python 3.10+，只用标准库 + `requirements.txt` 里的依赖。**不要引入新依赖**，
  确有必要请在 PR 中说明理由与体积代价。
- 所有面向用户的输出用中文，注释用中文，变量名与函数名用英文。
- 脚本开头写清用法 docstring；面向用户的 `print` 用 `[模块名]` 前缀。
- Windows 上 `stdout` 编码可能不是 UTF-8，涉及中文输出时按需 `sys.stdout.reconfigure(encoding="utf-8")`。

## 三条硬性纪律（踩过坑，请勿回退）

1. **图像读写必须走 `vds_common.imwrite_unicode` / `imread_unicode`。**
   直接用 `cv2.imread` / `cv2.imwrite` 在非 ASCII 路径（含中文）下会**静默失败**——
   返回 False 但不抛异常，结果是关键帧全空、接触表全黑。这个 bug 排查成本极高。

2. **测出来的数字要与术语的语义对齐，不能夸大。**
   例如 `light_ratio_stops` 是画面级明暗比，不是布光光比；`fg_bg_sharpness_ratio`
   只是景深弱线索。新增指标时若语义有边界，请在 `references/analysis_rules.md`
   与 README「已知限制」里写清楚。

3. **不要编造数据。**
   无语音识别时不得凭音频分类结果写出具体台词；不得为"让输出好看"而填充未测量的字段。

## 提交与 PR

- 提交信息用中文或英文均可，需说明**改了什么、为什么改**。
- 改动影响输出的，请在 PR 描述里附上 `--selftest` 的运行结果。
- 不要提交 `outputs/`、`.workbuddy/`、媒体文件或 `references/output_template.md`
  （已在 `.gitignore` 中排除，请勿用 `-f` 强制添加）。
- 不要提交任何本机绝对路径、用户名、凭据。

## 参与内容安全

本项目不联网、不上传数据，这是设计约束。**任何引入网络请求的改动都需要在 PR 中
单独论证**，说明必要性、传输内容与用户可关闭的方式。

## 提 Issue

- Bug：附 `--selftest` 输出、Python 版本、操作系统、`requirements.txt` 中各依赖版本。
- 效果问题：附视频的**元数据**（时长/分辨率/帧率）与问题镜头的接触表截图，
  不要附有可能涉及版权的完整视频。
- 新增指标建议：说明它回答什么问题、单位是什么、如何验证它测得准。

## 许可

提交即表示你同意以本项目的 [MIT License](LICENSE) 授权你的贡献。
若你的贡献涉及第三方素材（图片、视频、字体、模板），请确认其许可允许再分发，
并在 PR 中说明来源。
