---
name: stock-intelligence
description: >
  股票市场情报：X 舆情（Jev 叙事层：立场/作者质量/喊单检测/极化/事件抽取）×
  期权定价（IV/25Δ RR/期限斜率/PC OI/GEX）→ 四象限 Regime（对齐/背离×上下行）+
  拥挤度。一个 skill 内部模块化：--mode sentiment 只跑舆情深挖、--mode options
  只跑期权、--mode full 完整研究链。Use when the user names a stock ticker and
  asks 舆情/情绪/利多还是利空/X 上怎么看/sentiment/bullish or bearish/期权/
  叙事与定价/背离/四象限/regime/交易研究/怎么看这只股票. 输出永远是描述性市场
  状态，不是买卖信号；信息在最后一步以状态融合（alignment/divergence/crowding），
  绝不压成单一分数。
---

# Stock Intelligence（叙事 × 定价 → 市场状态）

一个 skill，三层模块，信息**晚融合、状态融合**：

```
① 叙事层  sentiment/   X 真实推文 → Jev 逐帖判断（立场/质量/喊单/作者/主张证据/
          极化/事件）→ 净利多指数 + 事件面板     "市场在说什么"
② 期权层  options/     yfinance 链快照 → ATM IV/25Δ RR/期限斜率/PC OI/朴素 GEX
          子状态三车道：direction（仅 RR+PC OI 判向）/ volatility / positioning
                                                        "市场在为什么付钱"
③ 融合层  fusion.py    状态四字段（narrative_state/options_state/alignment/
          crowding）+ STALE 门控 → Regime            "两个市场一致吗"
＋ 卖方共识 analysts.py yfinance 评级/目标价/PE —— 独立数据点，只展示不参与融合
＋ 数据质量 (schema 1.2) 逐源 level + data_as_of/age + 美股时段感知 + 门控
```

## 模式路由（按用户意图选）

| 用户问 | 模式 | 命令 |
|---|---|---|
| “看下 NVDA 的舆情”（只要舆情） | sentiment | `uv run run.py --ticker NVDA --mode sentiment` |
| “NVDA 期权什么样”（只要期权） | options | `uv run run.py --ticker NVDA --mode options` |
| “NVDA 现在怎么样”（默认综合） | full | `uv run run.py --ticker NVDA --mode full` |
| “跑一遍 watchlist” | all | `uv run run.py --mode all` |
| “我的组合现在什么状态” | portfolio | `uv run run.py --mode portfolio`（拥挤暴露/相关簇/温度榜，复用最新快照不重跑舆情） |
| “看时间序列/面板” | panel | `uv run panel.py [--ticker X] [--csv out.csv]`（版本一致去重 + 阈值校准统计） |

统一入口在 skill 根目录（**PowerShell 下用 cmd /c 外层单引号包裹**）：

```bash
cmd /c 'cd /d C:\Users\Roy\.agents\skills\stock-intelligence && uv run run.py --ticker NVDA --mode full'
```

- `full` 模式复用 <24h 的 v2 舆情快照（省 1-2 分钟和 API 成本）；过期/缺失自动
  补跑一次舆情再融合
- `sentiment` 模式等价于原 x-stock-sentiment 完整管线（opencli 真实抓帖，n 默认 50）
- 舆情模块细节（问题定义/权重公式/采样口径）见
  [references/sentiment-internals.md](references/sentiment-internals.md)；
  回测路线（A/B/C/D 模型、lag 纪律）见 [references/backtest.md](references/backtest.md)

## 前置

- 叙事层：`TYPESAFE_API_KEY`（Jev）；opencli + Chrome 扩展 + 登录 x.com
  （`opencli doctor` 检查；多 profile 时 `opencli profile use <chrome>`）
- 浏览器实例白名单（默认拒绝）：`browser.json` 绑定唯一生产浏览器 ——
  `allow_profiles: ["jbtu4mh6"]`（Chrome）、`chrome_path` 绝对路径（永不 `start chrome`）、
  `health_url: https://www.google.com/`（中性检查页，与业务源 x.com 分离）；
  环境变量 `OPENCLI_PROFILES`/`OPENCLI_CHROME_PATH` 可临时覆盖。非白名单实例
  （退役 Edge/vmawp5gu）被忽略并打印一次，绝不拉起/重试。
- skill 自带恢复链（有界）：无健康实例→按 `chrome_path` 拉起 Chrome→导航式健康检查→
  失败重试 1 次→仍失败计 1 次恢复失败，连续 2 次后熔断 fail-fast（不无限重启 Chrome）。
  铁律：`profile list/bind` 成功≠可用，只有真实导航成功才算健康。
- 期权层：yfinance 可达（免费、15 分钟延迟、盘外时段为冻结值）
- 卖方共识：同 yfinance（Yahoo 汇总口径，分桶与 TIKR/FactSet 可能不一致）
- KOL 名单：`sentiment/accounts.json`（boost 加权 / zero 剔除）

## 解读纪律（必须按此口径输出）

1. **净利多指数 ∈ [-1,+1]，中点是 0**；±0.15 内中性；不是上涨概率。采样置信度
   （High/Medium/Low）与指数分开报。默认不支持跨票比较；时间序列才是主要用法。
2. **信息不许过早融合**：叙事指标、期权指标各自独立呈现，Regime 是四个独立字段
   （narrative_state / options_state / alignment / crowding，含 relationship/
   pricing_direction 别名），最后只做状态融合（ALIGNED/DIVERGENT/MIXED/NOT_FUSED
   + 拥挤 HIGH/MEDIUM/LOW），不合成单一分数。regime_label 只是展示文案。
3. **DIVERGENT 是研究标记不是反向交易信号**：五解释逐一排查（X 样本偏差/
   影响有限/机构不认同/已提前定价/叙事无资金跟随）。
4. **事件面板未经验证**：高影响事件自动加⚠，交易决策前必须回源（公告/SEC/IR）。
5. **极端一致是描述性标记**（|净指数|>0.35 或单边>80%）：反转关系未回测，不得
   当反向规则。
6. 永远成立的边界：Sentiment≠预期收益·事件提及≠事实·作者专业≠主张为真·
   互动量≠信息质量·IV 高≠看涨（IV 是幅度不是方向）·GEX 是对冲结构不是涨跌预测
   ·卖方评级/目标价≠买卖信号（且是 Yahoo 口径的汇总，与第三方统计可能有出入）。
7. **数据质量门控（schema 1.2，P0）**：
   - ticker 预检：跑 Jev 前先验证 Yahoo 可解析，无效代码快速失败；
   - 期权 STALE 门：盘外冻结 >20h → options_state=STALE、alignment=NOT_FUSED、
     crowding=null（拒绝融合，宁可无 Regime 也不把冻结链误读为背离）；
   - 叙事置信门：独立作者 <10 或有效帖 <20 → 数据质量 LOW 并写入 gates；
   - 跨时段检查：叙事快照与期权数据不属于同一美股交易日 → gates 警告（周末/
     周一早间运行必触发，属正常诚实标记）。
8. **卖方共识只独立呈现**（覆盖数/评级分布/目标价/PE），不参与叙事×定价的
   状态融合，也不参与拥挤度判断。

## 数据与版本

- 每次运行版本化落盘：舆情 `data/sentiment/`，融合 `data/fusion/`
  （meta 带 schema/options/analyst/fusion/prompt 版本与 sampling_profile）
- 回测面板只收版本一致的行；改问题定义 bump PROMPT_VERSION，改权重/阈值 bump
  AGGREGATION_VERSION（详见 references）
- **fv2/ov2/schema1.2（2026-09-23）**：数据质量门控 + 期权子状态 + ticker 预检；
  方向判定规则与 fv1 完全一致（同一日同一票数值不变），但面板按版本分行——
  当日 fv1 快照为 v1 遗存，时间序列从 fv2 起算
- 时间序列只能从首次运行开始积累（X 无历史回填）；每日同口径跑 watchlist 是
  攒数据的主要方式；最佳跑窗口为美股开盘时段（北京 21:30-04:00），盘外跑
  期权为冻结值（≤20h 仅降级标注，>20h 拒绝融合）
