# X 上帖子的 Jev 情绪分析 —— 判断某只股票利多/利空

（本文件是 x-stock-sentiment skill 的架构参考；SKILL.md 是操作入口。）

从 X（或财经文章）拉取某只股票相关帖子 → Jev (TypeSafe System One) 逐帖给出
类型化判断（立场/真伪/催化剂/信息量/喊单嫌疑）→ 代码聚合出净利多指数。

## 用法

```bash
# Exa 财经内容（零配置，现在可用）
uv run run.py --ticker NVDA --source exa --n 6

# X 真实帖子（需 OpenCLI 扩展已装：https://chromewebstore.google.com/detail/opencli/ildkmabpimmkaediidaifkhjpohdnifk）
uv run run.py --ticker NVDA --source opencli --n 15

# 手动样本帖（文件里每行一帖，或用 --- 分隔多行帖）
uv run run.py --ticker NVDA --source paste --file posts.txt
```

需要环境变量 `TYPESAFE_API_KEY`。

## 设计

每帖一次 Jev 请求，5 个并行问题（speculative fan-out）：

| 问题 | 类型 | 用途 |
|---|---|---|
| stance | Choice | 强利多/利多/中性/利空/强利空/无关（取概率分布算期望） |
| genuine_view | Noul | 真实独立观点 vs 梗图/情绪/广告/复读 |
| has_catalyst | Noul | 是否含具体催化剂（财报/产品/订单/监管） |
| info_novelty | Score | 信息量：复读旧闻 → 原创分析 |
| promo_suspect | Noul | 疑似协同喊单/荐股水军 |

聚合（纯代码，不再推理）：

```
stance期望 = Σ p(选项) × 分值(强多+1 … 强空-1，无关=0)
帖子权重 w = genuine × (0.7+0.3·catalyst) × (0.5+0.5·novelty) × (1-0.8·promo)
净指数 = Σ(w × stance期望) / Σ w ∈ [-1, 1]
判定: >0.15 利多 / <-0.15 利空 / 其余中性
```

所有原始判断落盘 `data/`，供后续与真实涨跌对照校准（情绪信号 ≠ 交易信号）。

## 已知局限

- Exa 源取到的是财经文章（粒度粗于推文），OpenCLI 源才是 X 原帖
- 情绪→涨跌映射未验证，散户极端情绪历史上常为反向指标

## 架构细节（v2）

### 分层

```
L0 采集 (sources.py: opencli/exa/paste)
L1 质量门 (run.py dedup: 精确去重+模板去重，协同聚类计数)
L2 逐帖判断 (jev.py 两段式)
L3 聚合 (run.py aggregate: 指数/极化/置信度/占比)
L4a 论据/事件 (run.py reasons_summary + event_panel)
L5 报告 + 版本化落盘
```

### 两段式推理（TypeSafe dependent-question 模式）

第一段（每帖 9 问，fan-out 批量）：stance (Choice 6 项) / genuine_view /
has_catalyst (Noul) / info_novelty (Score) / promo_suspect (Noul) /
author_expertise (Score) / claim_type (Choice 6 项) / evidence_type (Choice 4 项) /
evidence_strength (Score)。
第二段（仅 has_catalyst≥0.5 的帖子，5 问）：event_type (Choice 8 类) /
event_direction (Choice) / event_impact (Score) / event_horizon (Choice) /
smart_money_named (Noul)。事件面板标题永远标"未经验证"，高影响事件自动加⚠。

### 权重公式 (aggregation v2)

```
w = 相关性(1-p无关) × genuine × (0.7+0.3·catalyst) × (0.5+0.5·novelty)
    × (1-0.8·promo)
  × (0.7+0.3·min(1, log10(1+likes)/4))          # 互动，万赞封顶
  × (0.7+0.3·expertise/2)                        # 作者专业度（Jev 读 bio+文风）
  × (0.7+0.3·min(1, log10(1+followers)/5.7))     # 触达，~50万粉封顶（--author-stats）
  × accounts.json 倍数 / 黑名单归零
```

stance 期望 = Σp(选项)×分值：强多+1 / 多+0.5 / 中性0 / 空-0.5 / 强空-1 / 无关0。
净指数 = Σ(w×期望)/Σw，判定阈值 ±0.15（初始值，待校准）。
极化 = 有效帖 stance 期望的加权标准差（>0.45 High / >0.3 Medium）。
采样置信度：有效帖≥15 且独立作者≥10 → High；≥8/≥6 → Medium；否则 Low。
样本不足守卫：总权重<1.0 或有效帖<5 → 判"样本不足"而非"中性"。

### 版本规则

改 jev.py 问题定义 → bump PROMPT_VERSION；改权重/阈值/聚合 → bump
AGGREGATION_VERSION；改落盘结构 → bump SCHEMA_VERSION。历史可比性依赖这些字段。

### 文档与原始来源

- TypeSafe API: https://docs.typesafe.ai/api.md（POST /v1/systemone，jev-latest）
- Primitives: https://docs.typesafe.ai/primitives.md（Choice/Score/Noul）
- Fan-out/parallel questions: https://docs.typesafe.ai/cookbooks/parallel_questions.md
- 数据源: agent-reach（opencli twitter search，X 原生操作符透传）

### 校准路线图（未做）

1. 同口径每日跑 → data/ 攒时间序列
2. 净指数与次日/次周收益对照，验证有无预测力（大概率要反向解读极端值）
3. 阈值 ±0.15 与权重系数用历史判断重算拟合（judgments 已落盘，无需重新推理）
