# ReviewHive

**完全运行于本地模型的代码评审多 Agent 协作平台。**

主 Agent 负责规划与调度，多个专家子 Agent（安全 / 性能 / 规范 / 测试 / 多模态）并行评审，评审知识通过 **Qdrant 向量检索 + Elasticsearch BM25** 混合召回并由 **bge-reranker** 精排。全流程不调用任何云端 API——所有模型都跑在你自己的机器上。

> 📖 使用方式、实现细节与原理图解见 **[GUIDE.html](GUIDE.html)**（自包含单文件，浏览器直接打开）。

## 特性

- **主 Agent 混合编排**：主干流水线固定（受理 → 规划 → 检索 → 并行评审 → 汇总），规划与汇总环节由主 Agent（Qwen3.6-35B-A3B MoE）动态决策：评审重点、调度哪些子 Agent、如何合并去重与校准严重度
- **Skills 技能体系**：子 Agent 通过 JSON 协议按需调用技能（`read_file` / `grep_code` / `search_kb` / `analyze_diff` / `read_image`），技能可注册、可自省（`GET /api/skills`）
- **混合 RAG**：Qdrant 语义召回 + Elasticsearch 关键词召回，RRF 融合，本地 bge-reranker-v2-m3 精排；知识库支持评审样例、漏洞模式、最佳实践三类语料
- **多模态评审**：Qwen3-VL 解读架构图/截图，与代码交叉验证（可选启用）
- **实时可观测**：Web UI 通过 SSE 实时展示主 Agent 调度过程、每个子 Agent 的技能调用与产出
- **链路追踪（可选）**：对接 Arize Phoenix（OpenTelemetry + OpenInference），每次评审的完整 span 树——会话 → 阶段 → 子 Agent → 每次 LLM 调用（prompt/补全/token/延迟）与技能调用——可在 `http://localhost:6006` 回放；关闭开关即退化为零开销
- **开箱即用**：内置 15 条高质量 Java 评审种子知识；支持一键下载微软 CodeReviewer、CodeXGLZ 缺陷数据集扩充知识库

## 架构

```
                    ┌──────────────────────────────────────────┐
   Web UI (SSE) ──▶ │              FastAPI 服务                 │
                    └──────────────┬───────────────────────────┘
                                   │
                    ┌──────────────▼───────────────┐
                    │   ReviewPipeline（固定主干）   │
                    │ 受理→规划→检索→并行评审→汇总    │
                    └──────────────┬───────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │ 动态规划 / 汇总       │ 混合检索              │ 并行评审
      ┌───────▼───────┐     ┌────────▼─────────┐   ┌─────▼─────────────┐
      │ Orchestrator  │     │ Qdrant + ES      │   │ 子 Agent × N       │
      │ Qwen3.6-35B   │     │ RRF 融合          │   │ security/perf/    │
      │ (llama-server)│     │ + bge-reranker   │   │ style/test/vision │
      └───────────────┘     └──────────────────┘   └────────┬──────────┘
                                                            │ Skills 循环
                                                   read_file / grep_code /
                                                   search_kb / read_image
```

**评审协议**：子 Agent 每轮只输出一个 JSON——`{"action":"skill", ...}` 调用技能补充信息，或 `{"action":"final", "findings":[...]}` 给出结构化结论。该协议与模型原生 tool-calling 能力解耦，对本地量化模型更稳健。

## 项目截图
<img width="2044" height="1013" alt="image" src="https://github.com/user-attachments/assets/f3159679-bc01-4964-9eab-6477418a98f3" />

<img width="2042" height="1013" alt="image" src="https://github.com/user-attachments/assets/71ac6c5d-aae3-4f92-ae1e-d641c8cc37b4" />


## 本地模型清单

| 角色 | 模型 | 服务方式 |
| --- | --- | --- |
| 主 LLM（规划/评审/汇总） | Qwen3.6-35B-A3B (MoE, Q4_K_M) | llama-server :8080 |
| 多模态 | Qwen3-VL-8B-Instruct (Q4_K_M) | llama-server :8082 + mmproj |
| 向量嵌入 | BAAI/bge-m3（默认）或 Qwen3-Embedding-8B | 进程内加载 / 可选独立端点 |
| 重排 | BAAI/bge-reranker-v2-m3 | 进程内加载 |

## 硬件要求与环境准备

### 最低硬件配置

| 组件 | 最低要求 | 推荐配置 | 说明 |
| --- | --- | --- | --- |
| CPU | Apple M1 / 同等 ARM64 或 x86_64 | Apple M1 Max 及以上 | llama.cpp 需 Metal 或 CUDA 加速 |
| 内存 | 32 GB | 64 GB | 主 LLM ~20GB + VL ~5GB + 嵌入/重排 ~4GB + ES ~2GB |
| 磁盘 | 50 GB 可用空间 | 100 GB SSD | 模型文件 ~25GB + Docker 镜像 ~8GB + 知识库数据 |
| GPU | 支持 Metal (macOS) 或 CUDA (Linux) | 统一内存 ≥ 40GB | `-ngl 999` 全层 offload 需显存/统一内存足够 |

### 内存占用明细（M1 Max 64GB 实测）

全量启动时各组件的常驻内存占用：

| 组件 | 进程 | 内存占用 | 说明 |
| --- | --- | --- | --- |
| 主 LLM | llama-server :8080 | ~20 GB | Qwen3.6-35B-A3B Q4_K_M，`-ngl 999` 全层 offload |
| 多模态 VL | llama-server :8082 | ~5 GB | Qwen3-VL-8B Q4_K_M + mmproj |
| 向量嵌入 | Python 进程内 | ~2 GB | bge-m3，首次加载后常驻 |
| 重排模型 | Python 进程内 | ~2 GB | bge-reranker-v2-m3，首次加载后常驻 |
| Elasticsearch | Docker 容器 | ~2 GB | 默认 JVM heap 1GB |
| Qdrant | Docker 容器 | ~1 GB | 向量索引 + 缓存 |
| FastAPI + 开销 | Python 进程 | ~1 GB | 应用本身 + asyncio |
| **合计** | | **~33 GB** | 评审并发时峰值可达 ~40 GB |

> 32GB 机器可运行但会频繁 swap，评审延迟显著增加。64GB 统一内存可全量常驻无压力，支持并发评审。

### 各模型磁盘占用

| 文件 | 大小 | 存放路径 |
| --- | --- | --- |
| Qwen3.6-35B-A3B-Q4_K_M.gguf | ~20 GB | `$HOME/.cache/modelscope/` |
| Qwen3VL-8B-Q4_K_M.gguf | ~5 GB | 同上 |
| mmproj-Qwen3VL-8B-Q8_0.gguf | ~1.5 GB | 同上 |
| bge-m3（HuggingFace 缓存） | ~2 GB | `~/.cache/huggingface/` |
| bge-reranker-v2-m3（HuggingFace 缓存） | ~2 GB | 同上 |
| Docker 镜像（Qdrant + ES + Phoenix） | ~8 GB | Docker 存储 |
| **合计** | **~38 GB** | |

> 仅运行静态扫描（`reviewhive review --scan-only`）无需 GPU，普通机器即可——该模式纯 stdlib，零 LLM 开销。

### 软件依赖

| 软件 | 版本要求 | 用途 | 安装方式 |
| --- | --- | --- | --- |
| Python | ≥ 3.10 | 运行时 | `brew install python` / 系统包管理器 |
| uv | 最新 | 包管理与虚拟环境 | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Docker Desktop | 最新 | Qdrant + Elasticsearch | [docker.com](https://www.docker.com/products/docker-desktop/) |
| llama.cpp | 最新（需编译） | 本地模型推理服务 | 见下方编译步骤 |

### 编译 llama.cpp

```bash
git clone https://github.com/ggml-org/llama.cpp.git
cd llama.cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release     # macOS 自动启用 Metal；Linux 加 -DGGML_CUDA=ON
cmake --build build --config Release -j $(sysctl -n hw.ncpu)
# 产物: build/bin/llama-server
```

### 下载模型文件

模型默认存放在 `$HOME/.cache/modelscope/hub/models`，可通过 `MODELS_DIR` 环境变量覆盖。

| 模型 | 文件大小 | 下载命令 |
| --- | --- | --- |
| Qwen3.6-35B-A3B-Q4_K_M.ggUF | ~20 GB | `modelscope download Abiray/Qwen3.6-35B-A3B-Q4_K_M-GGUF Qwen3.6-35B-A3B-Q4_K_M.gguf` |
| Qwen3VL-8B-Instruct-Q4_K_M.gguf | ~5 GB | `modelscope download Qwen/Qwen3-VL-8B-Instruct-GGUF Qwen3VL-8B-Instruct-Q4_K_M.gguf` |
| mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf | ~1.5 GB | `modelscope download Qwen/Qwen3-VL-8B-Instruct-GGUF mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf` |

嵌入模型（bge-m3）与重排模型（bge-reranker-v2-m3）由 Python 进程内加载，首次运行时自动从 HuggingFace 下载并缓存。

### 环境检查清单

启动前确认以下条件：

```bash
python3 --version       # ≥ 3.10
docker info             # Docker 正在运行
uv --version            # uv 已安装
which llama-server      # 或 $HOME/llama.cpp/build/bin/llama-server
```

## 快速开始

### 1. 启动基础设施与模型

```bash
make infra          # 启动 Qdrant + Elasticsearch（docker compose）
make models-start   # 启动主 LLM 与 VL 的 llama-server
```

### 2. 安装

```bash
make install        # uv venv && uv pip install -e ".[local-models]"
```

### 3. 构建评审知识库

```bash
make ingest                          # 种子语料 -> Qdrant + ES
# 可选①：下载官方文档语料（OWASP 安全指南、阿里 Java 规范、p3c）
make fetch-docs
# 可选②：下载开源数据集（约 500 条/数据集）
make download-data
make ingest                          # 三类语料统一切片入库（结构优先，块 ≤1500 字）
```

### 4. 启动

```bash
make serve          # http://localhost:9100
make trace          # 可选：启动 Arize Phoenix 链路追踪（http://localhost:6006）
```

打开浏览器，点「载入示例代码」→「开始评审」，即可看到主 Agent 调度四个专家并行评审的全过程。

### 常用命令

```bash
reviewhive health       # 检查 LLM / VL / Qdrant / ES 连通性
reviewhive ingest       # 重建知识库（种子 + 数据集 + data/docs 文档）
reviewhive fetch-docs   # 下载官方文档语料（OWASP / 阿里规范 / p3c）
reviewhive download-data --limit 500
make test           # 运行单元测试
```

## 项目结构

```
reviewhive/
├── config.py            # 配置加载（settings.yaml + 本地覆盖 + 环境变量）
├── models/              # 模型客户端：LLM / Embedding / Reranker / Vision
├── rag/                 # 可插拔切片、Qdrant、Elasticsearch、混合检索、文档加载、入库
├── skills/              # 技能注册表与内置技能
├── agents/              # 子 Agent 基类、角色配置、主 Agent 编排
├── core/                # 数据结构、流水线、SQLite 会话存储
├── observability.py     # 链路追踪：OTel/OpenInference → Arize Phoenix（可选）
├── api/                 # FastAPI + SSE + 静态前端
├── data/                # 数据集下载与转换
└── cli.py               # reviewhive 命令行
```

## 语料与数据致谢

- [microsoft/code_review](https://huggingface.co/datasets/microsoft/code_review)（CodeReviewer, ICSE 2022）：真实 PR 评审意见
- [microsoft/code_x_glue_cc_defect_detection](https://huggingface.co/datasets/microsoft/code_x_glue_cc_defect_detection)：函数级代码缺陷数据
- [OWASP Cheat Sheet Series](https://github.com/OWASP/CheatSheetSeries)（MIT）：安全编码指南精选 16 篇
- [阿里巴巴 Java 开发手册](https://github.com/mysterin/alibaba-java-specification)（Markdown 社区版）：正文版权归阿里巴巴，本项目仅作学习引用
- [alibaba/p3c](https://github.com/alibaba/p3c)（Apache-2.0）：编码规约规则文档
- 内置种子知识库 `data/corpus/seed_java.json`：基于 CWE 与 Java 工程实践整理
- [Arize Phoenix](https://github.com/Arize-AI/phoenix)（Elastic License 2.0）：可选的链路追踪服务端，独立容器运行

## 许可证

[MIT](LICENSE)
