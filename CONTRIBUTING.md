# 贡献 Veridrop

感谢考虑参与贡献!Veridrop 是 AGPL-3.0 开源项目 —— 你的修复和新增能帮到
整个 AI API 中转站生态。

## 快速链接

- **Issue / bug 报告**:[github.com/canarybyte/veridrop/issues](https://github.com/canarybyte/veridrop/issues)
- **在线服务**:[veridrop.org](https://veridrop.org) — 适合"这家中转站显示 X 但我以为是 Y"这种场景报告(贴 `/r/{job_id}` URL)
- **设计文档**:[DESIGN.md](DESIGN.md) — 整体架构概览
- **检测器细节**:[DESIGN.md §3 / §6](DESIGN.md) — 每个 detector 怎么工作

## 本地开发环境

```bash
git clone git@github.com:canarybyte/veridrop.git
cd veridrop

python3 -m venv venv
./venv/bin/pip install -e ".[dev,web]"

# 跑测试集(应该 240 个左右,全部 pass)
./venv/bin/pytest tests/ -v

# 启本地 Web 服务(http://localhost:8000)
VERIDROP_JOBS_DIR=/tmp/veridrop-dev ./venv/bin/uvicorn web.server:app --reload
```

## 欢迎哪些 PR

| 类型 | 例子 | 备注 |
|---|---|---|
| 🐛 **Bug 修复** | 长上下文检测的 false positive、tokenizer 边界、UI 状态错误 | 一定带回归测试 |
| 🔬 **新检测器** | cache_control 兑现验证、图像输入检测、system_fingerprint 检查 | 看 [DESIGN.md §6.2](DESIGN.md) 的 `ActiveDetector` / `PassiveDetector` 接口 |
| 🌐 **新协议支持** | Anthropic Bedrock、Vertex AI 原生、Mistral、DeepSeek 等 | 照着 `protocols/anthropic/` 的结构来,目标 ≥80% 测试覆盖 |
| 📊 **基线数据** | `data/baselines/` 里加新模型的官方真品基线 | 用 `bench.sh` 跑官方 API,只 commit 输出的 JSON |
| 📖 **文档 / FAQ** | 新问答、翻译、示例 | 中文 + 英文双语都欢迎 |
| 🎨 **UI / UX** | 移动端排版、可访问性、暗色模式 | 不要引入 JS 框架,保持 vanilla |

## 不在范围内的内容

- **反向破解中转站工具**:Veridrop 是用来**验证**中转站真伪,不是帮人绕过
  限速 / 封号。这类 PR 会被拒绝。
- **闭源企业扩展**:AGPL-3.0 要求作为服务运行的修改版必须也开源,试图加
  闭源 hook 的 PR 会被拒。

## 代码风格

- **Python**:PEP 8 但宽松。函数 / 模块注释解释**「为什么」**而非「做什么」
  — 看现有代码就懂调性,注释经常记录"踩过的坑 / 防御的边界场景"。
- **测试**:pytest + `pytest-asyncio` 跑 async。外部 API 调用必须 mock,
  单元测试里**永远**不调真上游。
- **检测器实现模板**:`protocols/anthropic/detectors/identity.py` 是最小
  规范实现。`run()` 保持短,辅助函数放下面用 `_underscored_helpers()`。
- **Commit message**:祈使句(`fix:`、`feat:`、`docs:`、`chore:` 前缀)。
  中英文都可以。

## PR 流程

1. **重大改动先开 issue 讨论** — 设计有分歧时,issue 阶段聊清楚比 PR 改第三遍快得多
2. **一个 PR 干一件事**。多件不相关的改动会被请求拆分
3. **测试本地必须通过**(`./venv/bin/pytest tests/`),CI 会复测
4. **用户可见的行为改动要更新文档** — README、FAQ 或代码 docstring

## Bug 报告

issue 时带上:

- **期望行为**:比如「我发了 X 给中转站 Y,期待 `pass`」
- **实际行为**:比如「拿到 `fail`,summary 写'Z' — 但 Y 是官方 anthropic.com」
- **`/r/{job_id}` URL**(如果在 veridrop.org 上跑过)
- **完整 JSON**(如果本地跑过 `detect -o report.json`)

实测数据是修 bug 最快的路径 —— 我们今年发现的真 bug 一半都来自有人跑了
真实中转站、注意到反常输出。

## 安全

如果你发现安全漏洞(比如能让服务记录 key、注入向量等),**请不要开公开
issue**。详见 [SECURITY.md](SECURITY.md) 的负责披露流程。

## 许可证

提交 PR 即同意你的贡献以 **AGPL-3.0-or-later** 授权(跟项目其他部分一致)。
我们**不要求 CLA**。
