# 2026-06-13 · 银行股频繁买卖演示场景（设计 spec）

> 在 `examples/session_scenarios.py` 增 4 个聚焦银行股、高换手、操作丰富的演示场景，
> 喂二期看板的换手率/仓位/分布图表。纯客户端脚本，零引擎/服务改动。

## 范围（用户拍板）

- 扩充现有 `examples/session_scenarios.py`（不新建文件）。
- 4 个场景：`bank_rotate`（日线轮动）/ `bank_pyramid`（分钟分批建减仓）/ `bank_limit`（限价单+撤单）/ `bank_frenzy`（满仓轮动狂点）。
- 尊重 A 股 T+1：当日买入不可当日卖；所有卖单只针对已跨日解锁的持仓。
- 收尾：每场景 close 后打印 summary + 看板详情页链接（`/ui/#/session/<id>`）便于看六页签图表。

## 既有事实

- 服务跑容器 8766，写接口需 `VORTEX_BACKTEST_TOKEN`（脚本已读 env，本地直读模式无 `/data`）。
- 10 只银行股均有 82 交易日分钟数据，窗口 2026-02-02~2026-06-09，价档 民生 3.5 → 招商 37。
- advance 语义：`orders[*]` 带 `trade_date`+`exec_time` → A 语义停泊到该日 at-or-after 该分钟首个 bar
  （成交日确定，最稳）；`limit_price` 撮合时不满足 → 拒单（非挂单簿）；`cancel` 撤 open_orders 里
  next_bar 停泊、目标 bar 未到的单（撤单-only = 提交 order 时 `to` 取当前 sim_time 不推进）。
- 交易日列表脚本内置常量（本地直读拿不到日历），轮动日取窗口内真实交易日抽样。

## 4 场景设计

| 场景 | level | 流程 | 演示点 |
|---|---|---|---|
| `bank_rotate` 日线轮动 | daily | 建仓 3 只 → 每个轮动日卖出全部持仓 + 买入轮换的下 3 只，跨 ~12 个真实交易日 | 多标的日线轮动、高换手、仓位/月度图 |
| `bank_pyramid` 分钟分批建减仓 | 1min | 单只：D1 多个 exec_time 金字塔建仓（数量递增），D2 多个 exec_time 分批减仓 | 分钟粒度、this_bar 精确成交时点 |
| `bank_limit` 限价单+撤单 | 1min | 正常买一笔 → 挂 limit 过低买单（撮合即拒，打印 reason）→ 提交 next_bar 停泊单不推进 → cancel 撤掉 | limit_price 校验 + cancel |
| `bank_frenzy` 满仓轮动狂点 | daily | D0 一次买齐 10 只（均分 2000 万）→ 之后每个交易日卖 3 只买 3 只持续轮换 | 极高换手喂分布图表 |

- 初始资金 2000 万（容纳满仓 10 只）。
- 共用常量：`BANKS`（代码+中文名）、`ROTATE_DAYS`/`FRENZY_DAYS`（窗口内真实交易日抽样）。
- 复用现有 `_post/_get/_account/_open`；`_report` 增强打印看板链接。

## 错误处理 / 验证

- 拒单（T+1/涨跌停/limit 不满足/no_market_data）不崩脚本，按现有 `main()` 的 try/except 兜底打印。
- 实现后对容器 8766 逐场景实跑（带 .env token），确认成交、换手非零、看板能出图；
  拒单异常当场诊断调整。

## 范围边界

- 零引擎/服务/端点改动；纯 examples 脚本 + README 场景表。
- 本地直读模式（不依赖 `/data`，故不含网关算子下推场景——已由现有 scan 场景覆盖）。
