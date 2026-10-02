# ModelTrace 独立自动测试

## 用途

ModelTrace 是 Veridrop 之外的一种独立模型归因方式。它不加入 Anthropic、OpenAI 或 Gemini 协议 runner，不产生协议 `total_score`、`verdict`，也不进入现有 leaderboard。

网页入口：`/modeltrace`。历史记录入口：`/modeltrace/leaderboard`。

## 自动请求

表单只需要：

- `base_url`：目标 API 根地址；
- `api_key`：只用于本次任务；
- `model`：构造请求时使用的模型名，同时作为结果页的“预期模型”。

提交后适配层调用上游 `enrollment.test_automatic`。它会自动尝试 OpenAI Chat Completions 与 Anthropic Messages 格式，最多执行 6 次挑战请求，目标获取 3 份满足数字数量阈值的有效回答。失败、拒答、截断和有效数字不足的回答会记录到自动请求诊断，不会被伪装为成功回答。

服务端使用 `asyncio.to_thread` 执行同步上游请求，避免阻塞 FastAPI 事件循环；不启动上游 Flask 应用。

## 结果含义

结果页和 JSON 报告保留上游字段：

- `expected_model`：用户选择的请求模型，仅用于构造请求和显示；
- `prediction_name`、`probability`：统一候选库中最可能模型和归因概率；
- `family_prediction_name`、`family_probability`：最可能模型家族和家族概率；
- `used_outputs`：实际纳入分析的有效回答数；
- `results`：候选模型概率表及相似度；
- `api_test`：请求数量、尝试数量、有效回答数量和脱敏错误诊断；
- `source`、`source_revision`、`source_commit`、`license`：来源、可复现版本状态和许可证说明。

`probability` 和 `family_probability` 是上游统一候选库的独立统计值，不是 Veridrop 协议合规分、质量分或中转站排名分。`expected_model` 不参与候选排序或概率计算。

## 环境变量

CLI 和服务端默认值统一为：

```text
BASE_URL=
API_KEY=

ANTHROPIC_MODEL=
OPENAI_MODEL=
GEMINI_MODEL=
MODELTRACE_MODEL=
```

网页只用 `BASE_URL` 和 `MODELTRACE_MODEL` 填充 placeholder/default，不把服务端 `API_KEY` 回填到浏览器。CLI 的参数优先于环境变量。provider-specific 的 base URL 和 API key 环境变量不再作为配置来源。

## API key 生命周期

原始 API key 只在任务运行期间由内存中的 Job 和线程调用持有；报告只保存脱敏值，错误文本会移除传入的 key。任务完成或失败后不会把原始 key 写入 JSON、日志、JPG 或网页。

## 来源和许可证

源码来自 [xqy2006/ModelTrace](https://github.com/xqy2006/ModelTrace)，完整上游快照位于 `third_party/ModelTrace/`，保留上游 `LICENSE`、README、数据文件和插件文件。上游许可证为 MIT，版权归上游作者。Veridrop 的适配、任务、报告和页面代码位于 `src/relay_detector/modeltrace/`、`web/` 和 `web/templates/`。

本工作树使用用户提供的 `ModelTrace-main.zip`。通过 GitHub API 将 ZIP 中 `enrollment.py`、`fingerprint.py`、`bank_builder.py`、`unified_bank.json`、README 和 LICENSE 的 Git blob 与上游提交核对，确定对应提交为 `d4131b30243dfa05e70180b5eedde742103f1d73`。ZIP 本身不包含 Git 元数据，因此本地仍是可审查源码快照，不是保留上游完整提交图的 subtree 历史。

## 未验证范围

本项目测试使用 mock HTTP/上游函数验证协议识别、重试、失败处理、字段保留和页面分支；不执行真实上游 API 请求，也不把 mock 验证描述为真实服务验证。
