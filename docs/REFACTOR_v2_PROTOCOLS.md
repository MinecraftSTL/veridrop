# Veridrop v2 重构设计: 三套独立产品共享一个品牌

> **状态**: v0.2 草案,已完成第一轮架构 review 修订  
> **作者**: claude(代笔) + canarybyte(评审)  
> **执行原则**: 先落 Phase 0 的安全重构,不在同一批变更里上线 OpenAI/Gemini 检测

---

## 0. TL;DR

Veridrop 从单一 Claude 检测工具演进为三套产品入口: Claude、OpenAI、Gemini。三者共享品牌、Web 基础设施、任务队列、通用报告模型和检测器接口,但协议实现、检测项、报告模板、baseline、文案语境保持隔离。

v0.2 修正了 v0.1 的关键问题:

- 不再宣称进程级“物理隔离”,改为**运行时任务隔离 + 路由级熔断**。
- `BaseDetector / ActiveDetector / PassiveDetector` 进入 `core/detectors_base.py`,所有协议 detector 继承同一个 core 接口。
- Anthropic comparator 不能进 core,留在 `protocols/anthropic/comparator.py`。
- core 只提供 `comparator_framework.py`,各协议注册自己的 detector diff rules。
- OpenAI 首版不主打“真伪”,只主打协议合规、能力完整性、usage/token 与官方 baseline 的字段差异。
- 老 `src/relay_detector/openai/` Phase 0 不迁移,保留 legacy CLI 能力。

---

## 1. 动机

| 维度 | 现状(v1) | 目标(v2) |
|---|---|---|
| 协议范围 | 仅 Anthropic 主流程 | Anthropic + OpenAI + Gemini |
| URL 结构 | 全在 `/` | `/claude` `/openai` `/gemini` + hub |
| 检测逻辑 | Anthropic 假设散落在主包 | 协议实现放进 `protocols/<name>/` |
| 共享层 | 无明确边界 | `core/` 只放协议无关接口和框架 |
| 分级文案 | 容易误读成全是加密级验证 | 报告页强制展示 tier banner |
| baseline | 单目录 | `data/baselines/<protocol>/` |
| Web 路由 | 单体 `server.py` | 协议 router + lazy load |
| 熔断 | 全部一起挂 | 协议路由可用 feature flag 关闭 |

Claude 的 thinking signature 是 Veridrop 的最高可信度资产。OpenAI/Gemini 没有同等级的加密签名信号,所以 UI 和报告必须明确告诉用户:不同协议的 100 分含义不同。

---

## 2. 现状(v1)

### 2.1 现有目录树

```text
src/relay_detector/
├── cli.py
├── client.py                   # AnthropicClient + ThrottledClient
├── runner.py                   # 依赖 Anthropic client/config/detector base
├── scorer.py
├── comparator.py               # Anthropic 专属 diff rules
├── models.py
├── config.py                   # Anthropic model table + weights
├── report.py
├── detectors/                  # 10 个 Anthropic detector
├── data/                       # Anthropic 探测数据
└── openai/                     # legacy: validate / baseline CLI 已在用

web/
├── server.py                   # FastAPI 单体
├── jobs.py                     # Job 无 protocol 字段
├── image_report.py             # 只懂 Anthropic detector label
└── templates/                  # 只懂 Claude 表单和结果
```

### 2.2 当前硬编码点

- `client.py` 写死 `/v1/messages`、`x-api-key`、`anthropic-version`。
- `config.py` 写死 Claude 模型能力表。
- `runner.py` 用 `isinstance(d, ActiveDetector)` 和 `PassiveDetector` 分流。
- `comparator.py` 写死 thinking、PDF、toolu_、Anthropic 文案和 `models_match()`。
- detector 数据用 `importlib.resources.files("relay_detector.data")`。
- `pyproject.toml` 只把 `relay_detector/data/*.json|*.pdf` 打进 wheel。
- Web result/image report 假设固定 10 项 Anthropic detector。

---

## 3. 目标架构(v2)

### 3.1 URL

| URL | 作用 |
|---|---|
| `/` | Hub 页,展示 Claude/OpenAI/Gemini 三个入口 |
| `/claude` | Claude 检测表单、FAQ、cryptographic tier 文案 |
| `/openai` | Phase 1 占位页;Phase 2 Chat Completions 检测 |
| `/gemini` | Phase 1 占位页;Phase 3 Gemini 检测 |
| `/api/detect/claude` | Claude 检测提交 |
| `/api/detect/openai` | OpenAI 检测提交 |
| `/api/detect/gemini` | Gemini 检测提交 |
| `/api/status/{id}` | 通用 job status |
| `/r/{id}` | 按 report/job protocol 选择结果模板 |
| `/r/{id}.jpg` | 按 protocol 选择 JPG 报告渲染 |

Phase 0 只保证 `/claude` 和兼容旧 `/api/detect` 工作。`/openai`、`/gemini` 可在 Phase 1 作为占位页上线。

### 3.2 Python 目录

```text
src/relay_detector/
├── core/
│   ├── __init__.py
│   ├── models.py                  # Protocol / DetectionReport / DetectorResult / Mode
│   ├── detectors_base.py          # BaseDetector / ActiveDetector / PassiveDetector
│   ├── runner.py                  # 协议无关调度框架
│   ├── scorer.py                  # 通用加权评分
│   ├── comparator_framework.py    # 通用 diff dataclass + registry 框架
│   └── http_base.py               # 可选: 通用 retry/SSE 工具,Phase 0 可不做
│
├── protocols/
│   ├── anthropic/
│   │   ├── __init__.py
│   │   ├── client.py              # AnthropicClient + ThrottledClient
│   │   ├── runner.py              # Anthropic Runner wrapper,注入 MODE_DETECTORS/TTFT probe
│   │   ├── config.py              # Claude model table + weights + mode membership
│   │   ├── comparator.py          # Anthropic 专属 baseline diff rules
│   │   ├── detectors/
│   │   │   ├── __init__.py
│   │   │   ├── base.py            # 只 re-export core.detectors_base,不定义新类
│   │   │   ├── identity.py
│   │   │   ├── thinking_signature.py
│   │   │   └── ...
│   │   └── data/
│   │       ├── __init__.py
│   │       ├── behavioral_signatures.json
│   │       ├── knowledge_questions.json
│   │       └── test_document.pdf
│   │
│   ├── openai/                    # Phase 2 新主包
│   └── gemini/                    # Phase 3 新主包
│
├── openai/                        # Phase 0 保留 legacy 包,Phase 2 再迁移
├── cli.py
└── __init__.py
```

兼容 shim:

- `relay_detector.models` -> `relay_detector.core.models`
- `relay_detector.scorer` -> `relay_detector.core.scorer`
- `relay_detector.runner` -> `relay_detector.protocols.anthropic.runner`
- `relay_detector.client` -> `relay_detector.protocols.anthropic.client`
- `relay_detector.config` -> `relay_detector.protocols.anthropic.config`
- `relay_detector.comparator` -> `relay_detector.protocols.anthropic.comparator`
- `relay_detector.detectors.*` -> `relay_detector.protocols.anthropic.detectors.*`

这样现有 CLI、测试和外部脚本不会在 Phase 0 断掉。

### 3.3 数据模型

```python
class Protocol(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"

class DetectionTier(str, Enum):
    CRYPTOGRAPHIC = "cryptographic"
    BEHAVIORAL = "behavioral"
    PROTOCOL = "protocol"

class DetectionReport(BaseModel):
    protocol: Protocol = Protocol.ANTHROPIC
    tier: DetectionTier = DetectionTier.CRYPTOGRAPHIC
    tier_title: str = ""
    tier_message: str = ""

    base_url: str
    api_key_masked: str
    target_model: str
    mode: Mode
    timestamp: datetime
    total_score: float
    verdict: Verdict
    results: list[DetectorResult]
    performance: PerformanceMetrics
    summary: str = ""

    self_reported_identity: str | None = None
    detected_non_anthropic_brands: list[str] = Field(default_factory=list)
```

`protocol` 和 `tier` 提供默认值是为了兼容历史 JSON。新报告必须由协议包显式传入。

### 3.4 协议包契约

每个协议包导出普通函数,但 detector 类型必须来自 `core.detectors_base`:

```python
PROTOCOL_NAME: Protocol
TIER: DetectionTier

def model_choices() -> list[str]: ...
def default_model() -> str: ...
def build_config(mode: Mode, max_concurrent: int = 3) -> ExecutionConfig: ...
def build_detectors(mode: Mode | None = None) -> list[BaseDetector]: ...
def make_client(base_url: str, api_key: str, timeout: float): ...
def build_runner(client, detectors: list[BaseDetector], config: ExecutionConfig): ...
def baseline_path(model_id: str, mode: Mode) -> Path | None: ...
def verdict_caption(score: float) -> str: ...
def tier_banner() -> tuple[str, str]: ...
```

OpenAI/Gemini 不允许 import `protocols.anthropic.*`。允许 import 的共享层只有 `relay_detector.core.*`。

### 3.5 Tier banner 规范

每个协议结果页顶部必须显示 tier banner,不能只放在 FAQ。

Claude:

> 加密级验证: Claude thinking signature 来自 Anthropic 服务端签名。通过该项时,它是当前检测集中最高可信度的真伪信号。

OpenAI:

> 行为/协议级验证: 本检测无法可靠区分 GPT-4o 真品与 GPT-4o-mini 伪装。我们检测的是中转站接口是否符合 OpenAI 协议规范、能力是否完整、usage/token 是否与官方 baseline 接近。

Gemini:

> 协议级验证: 本检测主要验证 Gemini API 兼容性、关键能力和 usage 字段,不提供加密级模型真伪证明。

---

## 4. 路由级熔断

不要在 `web/server.py` 顶层 import 所有协议 router。按 feature flag 懒加载:

```python
ENABLED = [
    p.strip()
    for p in os.environ.get("VERIDROP_PROTOCOLS", "anthropic").split(",")
    if p.strip()
]

for proto in ENABLED:
    try:
        module = importlib.import_module(f"web.routers.{proto}")
        app.include_router(module.router)
        logger.info("loaded protocol router: %s", proto)
    except Exception:
        logger.exception("failed to load protocol router: %s", proto)
        # 不抛出,继续加载其他协议
```

这只保证 router import/runtime 层面的熔断,不保证进程级隔离。若某协议引入破坏全局依赖的包版本,仍可能影响整个服务。需要进程级隔离时,另起独立 service。

---

## 5. 隔离原则

| 层 | 规则 |
|---|---|
| 代码 | 协议包之间不得互相 import,只允许依赖 `core` |
| detector base | 所有 detector 继承 `core.detectors_base`,避免 `isinstance()` 失效 |
| comparator | core 只放框架,协议 diff rules 留在协议包 |
| data | `protocols/<name>/data` 独立打包 |
| baseline | `data/baselines/<protocol>/` |
| jobs | `web_data/jobs/<protocol>/` |
| router | `/api/detect/<protocol>` 独立 |
| 依赖 | 新协议重依赖放 extras,默认部署不强制安装 |
| 熔断 | `VERIDROP_PROTOCOLS` 控制启用协议 |

CI 增加 `tests/test_isolation.py` 或脚本检查:

- `protocols/openai/` 不出现 `protocols.anthropic`
- `protocols/gemini/` 不出现 `protocols.anthropic` 或 `protocols.openai`
- `core/` 不 import 任意 `protocols.*`

---

## 6. Phase 0: 安全重构,不加新协议

### 6.1 准备

必须开分支,不能直接在 main 改:

```bash
git switch -c refactor/v2-protocols
pytest tests/ -v
```

若 `refactor/v2-protocols` 与现有 Git ref 冲突,使用 `codex/refactor-v2-protocols`。

### 6.2 创建 core 与协议目录

```bash
mkdir -p src/relay_detector/core
mkdir -p src/relay_detector/protocols/anthropic
mkdir -p src/relay_detector/protocols/openai
mkdir -p src/relay_detector/protocols/gemini
```

移动:

```bash
git mv src/relay_detector/models.py     src/relay_detector/core/models.py
git mv src/relay_detector/scorer.py     src/relay_detector/core/scorer.py
git mv src/relay_detector/runner.py     src/relay_detector/core/runner.py

git mv src/relay_detector/client.py     src/relay_detector/protocols/anthropic/client.py
git mv src/relay_detector/config.py     src/relay_detector/protocols/anthropic/config.py
git mv src/relay_detector/comparator.py src/relay_detector/protocols/anthropic/comparator.py
git mv src/relay_detector/detectors     src/relay_detector/protocols/anthropic/detectors
git mv src/relay_detector/data          src/relay_detector/protocols/anthropic/data
```

新增:

- `core/detectors_base.py`: 从旧 `detectors/base.py` 抽出 base classes。
- `protocols/anthropic/detectors/base.py`: 只 re-export core base classes。
- `core/comparator_framework.py`: dataclass + registry,不含 Anthropic 文案。
- legacy shim 文件,保持旧 import path 可用。

### 6.3 package-data 与 resources

`pyproject.toml` 必须改:

```toml
[tool.setuptools.package-data]
"relay_detector.protocols.anthropic" = ["data/*.json", "data/*.pdf"]
```

所有 Anthropic detector 的资源路径改:

```python
resources.files("relay_detector.protocols.anthropic.data")
```

并给 `protocols/anthropic/data/__init__.py` 加空文件,确保 package resource 稳定。

### 6.4 legacy OpenAI

Phase 0 不移动 `src/relay_detector/openai/`。新增:

```text
src/relay_detector/openai/_LEGACY.md
```

内容说明: 该包仍服务 `relay-detector openai validate/baseline`;Phase 2 再迁移到 `protocols/openai`,旧路径保留 re-export shim。

### 6.5 Web 兼容

Phase 0 最小目标:

- `/claude` 渲染现有表单。
- `/api/detect/claude` 调用 Anthropic job。
- 旧 `/api/detect` 保留,内部走 Claude,响应加 `Deprecation` header。
- 老 job JSON 缺 `protocol` 时默认 `anthropic`。
- JPG report 支持 `protocol="anthropic"` 参数。

完整 hub、OpenAI/Gemini 占位页放 Phase 1。

### 6.6 验证

```bash
pytest tests/ -v
relay-detector --help
relay-detector openai --help
relay-detector detect --help
```

部署验证:

- `/claude` 200。
- `/api/detect/claude` 可提交。
- 老 `/api/detect` 仍可提交。
- `/r/{historic_id}` 对缺 `protocol` 的报告仍按 anthropic 渲染。
- JPG 报告可生成。

---

## 7. Phase 1: Hub 和占位页

新增:

- `web/routers/hub.py`
- `web/routers/openai.py` coming-soon
- `web/routers/gemini.py` coming-soon
- `web/templates/hub.html`
- `web/templates/openai/_coming_soon.html`
- `web/templates/gemini/_coming_soon.html`
- `web_data/wishlist.txt`

邮箱收集文案:

> 邮箱仅用于 OpenAI/Gemini 检测上线通知,不与第三方共享,每封通知邮件包含退订方式。

---

## 8. Phase 2: OpenAI Chat Completions 检测

定位: **协议合规 + 能力完整 + baseline 字段差异**。不宣称加密级真伪判断。

首版只做 Chat Completions。Responses API 放 Phase 2.5。

检测项:

| Detector | 目的 |
|---|---|
| `basic_request` | 最小请求可用,能提取文本 |
| `model_consistency` | `response.model` 与请求模型匹配,多次输出稳定 |
| `function_calling` | tool call 结构、`call_` ID、arguments JSON |
| `structured_output` | JSON schema / response_format 能力 |
| `protocol` | `chatcmpl-`、`chat.completion`、choices、finish_reason、usage |
| `integrity` | stream vs non-stream 文本和 usage 一致性 |
| `token_parity` | 与官方 baseline 的 input/output/total tokens 字段差异 |

删除 v0.1 的 `knowledge` detector。通用公司常识题不能支撑 OpenAI 真伪叙事。

---

## 9. Phase 2.5: OpenAI Responses API

在 Chat Completions 稳定后再加入:

- `/v1/responses` 非流式结构校验。
- Responses streaming SSE。
- Responses function_call / structured output。
- 官方 baseline 分目录保存。

---

## 10. Phase 3: Gemini

Gemini 首版定位为协议级验证,检测 API shape、function calling、streaming、usage 和基础能力。不做加密级真伪承诺。

---

## 11. CLI

新增 `veridrop` console script,保留 `relay-detector`:

```toml
[project.scripts]
relay-detector = "relay_detector.cli:app"
veridrop = "relay_detector.cli:app"
```

Phase 0 旧命令仍工作:

```bash
relay-detector detect ...
relay-detector compare report.json
relay-detector openai validate ...
relay-detector openai baseline ...
```

后续新增:

```bash
veridrop claude detect ...
veridrop openai detect ...
veridrop gemini detect ...
veridrop compare report.json
```

---

## 12. 测试策略

Phase 0:

- 现有 unit tests 全过。
- 增加 legacy import 测试:旧路径和新路径都能 import。
- 增加 isolation 测试:core 不 import protocols,protocols 不跨协议 import。
- 增加 resources 测试:behavioral signatures / knowledge questions / PDF 能从新 package path 加载。

Phase 1:

- `/` hub 200。
- `/claude` 200。
- `/openai` `/gemini` coming-soon 200。
- wishlist 写入不影响检测主流程。

Phase 2+:

- OpenAI client/protocol template 单测。
- Chat Completions full run mock tests。
- 官方 baseline compare fixture。

---

## 13. 回滚

| 阶段 | 回滚方式 |
|---|---|
| Phase 0 代码重构出问题 | 切回 main 或 DNS 回旧服务 |
| Phase 1 hub 影响转化 | `/` 重新 302 到 `/claude` |
| OpenAI 误报严重 | `VERIDROP_PROTOCOLS=anthropic,gemini` 并重启 |
| Gemini 依赖/协议问题 | `VERIDROP_PROTOCOLS=anthropic,openai` 并重启 |

---

## 14. 已决策问题

| 问题 | 决策 |
|---|---|
| Q1 `/` 是 hub 还是 Claude 页 | Hub |
| Q2 评分是否各协议独立 | 独立 0-100,强制显示 tier badge |
| Q3 历史 job 是否迁移 | 不重写磁盘,loader 默认缺省为 anthropic |
| Q4 CLI 名称 | 新增 `veridrop`,保留 `relay-detector` |
| Q5 是否 main 直改 | 不允许,必须开 refactor 分支 |
| Q6 tier 文案放哪里 | 报告页顶部 banner 必须显示 |
| Q7 OpenAI 支持哪条协议 | Phase 2 先 Chat Completions,Responses 留 Phase 2.5 |
| Q8 是否收集邮箱 | 收,但加最小隐私说明 |

---

## 15. Out of Scope

- 用户账号、登录、API 配额。
- 排行榜、公开 leaderboard。
- 付费订阅。
- 英文版 UI。
- 进程级多服务隔离。
- OpenAI/Gemini 加密级真伪验证。

