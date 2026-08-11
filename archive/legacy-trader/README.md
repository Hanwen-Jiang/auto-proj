# Legacy A6 Trader Archive

本目录冻结整理前的独立 Python Trader。它用于解释和迁移旧行为，不再作为新功能的开发入口。

## 快照内容

- `src/a6api_auto/`：账号注册、A6 客户端、Collector、Seller、Provider、定价、恢复、资金和面板代码。
- `scripts/`：生产 reconcile、修复、回填、审计与发布脚本。
- `tests/`：旧模块的单元、集成、DOM 和部署合同测试。
- `migrations/`：`a6_trade` schema 的 0001-0069 迁移。
- `deploy/`：旧 systemd、日志轮转和区域部署定义。
- `docs/`：旧设计稿、审阅记录、事故记录和完整旧 API Reference 副本。
- `LEGACY_README.md`：整理前的完整运行与部署说明。
- `TEST_BASELINE.md`：冻结快照的可重复测试命令和已知失败基线。

## 归档性质

快照由 Git 基线 `ae30382` 加上整理时尚未提交的 Trader 演进工作树组成。活跃协议已经移到仓库
根目录，发生冲突时以根目录 `docs/a6-api-reference.md`、实现 Prompt 和 `errorlist.md` 为准。

以下运行数据按仓库规则留在本机 `.local/legacy-trader/`：

- 商户恢复代理池。
- USDT 钱包 inventory。
- HAR、运行输出、缓存和虚拟环境。

因此依赖真实 treasury inventory 的少量历史测试需要在受控环境恢复对应本地文件后运行。源码、
控制器、迁移和普通测试均保留。

## 阅读顺序

1. 根目录 `docs/legacy-trader-logic.md`。
2. 本目录 `LEGACY_README.md`。
3. `docs/a6-trading-system-design.md` 和专题设计稿。
4. 对应 `src/a6api_auto/trade_*.py`、脚本、迁移和测试。

归档代码中的历史假设不是新协议合同。例如旧工作树中出现过未经 A6 抓包确认的
`per_request_price_micros`；新实现应遵循根目录 API Reference 的 per-token Listing 合同。
