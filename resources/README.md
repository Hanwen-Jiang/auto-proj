# Resources

`corpora/` 是新 A6 Channel 模块公共展示文本的唯一仓库内来源：

- `merchant_templates_split.xlsx`：Supplier、Channel 和 Listing 文案。
- `token_name_corpus.json`：A6 Token 与 Sub2API Key 显示名。

实现时应保存语料文件 SHA-256、sheet、源行或数组索引、选择 generation 和原始文本。随机选择只
决定语料项，不生成新文本；重试复用同一次选择。

这些文件从旧 Trader 冻结快照中抽出。归档内保留原副本是为了让旧代码结构完整，活跃实现和
文档一律引用本目录。
