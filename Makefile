.PHONY: help infra infra-down models-start models-stop models-status install install-all serve ingest download-data fetch-docs trace trace-down test

help: ## 显示帮助
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

infra: ## 启动 Qdrant + Elasticsearch
	docker compose up -d

infra-down: ## 停止基础设施
	docker compose down

models-start: ## 启动本地模型服务
	./scripts/start_models.sh start

models-stop: ## 停止本地模型服务
	./scripts/start_models.sh stop

models-status: ## 查看模型服务状态
	./scripts/start_models.sh status

install: ## 安装基础依赖（含本地嵌入/重排模型支持）
	uv venv && uv pip install -e ".[local-models]"

install-all: ## 安装全部依赖（含数据集下载、链路追踪与测试）
	uv venv && uv pip install -e ".[local-models,datasets,observability,dev]"

trace: ## 启动 Arize Phoenix 链路追踪（http://localhost:6006）
	docker compose -f docker-compose.observability.yml up -d

trace-down: ## 停止 Arize Phoenix
	docker compose -f docker-compose.observability.yml down

serve: ## 启动 Web 服务（默认 9100 端口）
	.venv/bin/reviewhive serve

ingest: ## 构建知识库（种子语料 -> Qdrant + ES）
	.venv/bin/reviewhive ingest

download-data: ## 下载开源评审/漏洞数据集
	.venv/bin/reviewhive download-data

fetch-docs: ## 下载官方文档语料（OWASP / 阿里 Java 规范 / p3c）
	.venv/bin/reviewhive fetch-docs

test: ## 运行单元测试
	.venv/bin/pytest
