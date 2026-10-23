# 科研版 · 数据溯源核验（lab）

独立工具，**跟聊天无关**，可以单独拷出去用。

## 干什么

把你论文/报告里写的数值，逐条对照实验记录，告诉你：

- 哪些**有出处**
- 哪些**已经过时**（被后续论文推翻）
- 哪些**对不上**
- 哪些**查不到**（未溯源）

## 三条命令

```
python src/verify_doc.py 报告.md data/records.jsonl   # 核验文档
python src/build_index.py data/corpus                  # 建论文索引
python src/search.py 温度                              # 查数据
```

## 记录格式

一行一条 JSON：

```json
{"id":"EXP-01","param":"退火温度","value":850,"unit":"℃","date":"2026-05-01","source":"EXP-2026-13","scope":"样品1"}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| id | ✅ | 唯一编号 |
| param | ✅ | 参数名（要与文档写法一致） |
| value | ✅ | 数值（纯数字） |
| unit | ✅ | 单位（℃ / nm / um / min / V / mA / cm-2 / eV / % …） |
| date | ✅ | 日期（支持 2026-05-01 / 2026/05/01 / 2026年5月1日） |
| source | ✅ | 来源标识 |
| scope | 建议 | 样本/批次——**同参数不同样本必须填不同值** |
| superseded_by | 可选 | 显式声明被谁取代 |

## 判定规则

| 情况 | 结果 |
| --- | --- |
| 命中当前值 | 有依据（附来源与日期） |
| 命中被覆盖的旧值 | 已过时（附新值） |
| 两者都不是 | 矛盾 |
| 没有该参数 / 单位不在词表 | 未溯源 |
| 记录未声明 scope | **不判新旧**（安全默认） |

## 自测

```
python src/verify.py --selftest       # 15 例边界
python src/verify.py --acceptance     # 64 条验收
python tests/final_smoke50.py         # 50 组盲测
python tests/gate_supersession.py     # 新旧冲突（1000 篇真实论文）
```

## 已知限制

单位词表封闭（约 70 项）、不做单位换算、数值精确相等无容差、参数名需与记录一致。

## 数据

data/corpus/ 是 1000 篇 arXiv 摘要（20 个领域），用于离线检索与压测。可以删掉，不影响核验功能。