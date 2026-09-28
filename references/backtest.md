# 回测路线（jev-options-signal）

本文件定义攒数据的目标 schema 与回测纪律。当前阶段（v1）只采集不回测。

## 融合行 schema（每票每天一行，data/*.json 已按此落盘）

```json
{
  "meta":     {"schema_version", "options_version", "fusion_version",
               "narrative_prompt_version", "narrative_run_at", "run_at"},
  "options":  {"spot", "atm_iv_near", "atm_iv_next", "term_slope_vpt",
               "rr25_vpt", "pc_oi", "pc_vol", "gex_usd_per_1pct", "quality_flags"},
  "narrative":{"net_index", "polarization", "bull_share", "n_effective",
               "unique_effective_authors"},
  "fusion":   {"narrative_state", "options_state", "alignment", "crowding"}
}
```

版本不同的行不可直接混用（options_version/fusion_version/narrative_prompt_version
必须一致才进同一回测面板）。

## Lag 纪律（防内生性）

同一价格运动会同时推高舆情和期权活动，所以只允许：

```
X(t)  →  Return(t+1), Return(t+5), Return(t+20)     允许
X(t)  →  Return(t)                                    禁止（同期反应，伪预测）
```

Return 口径：收盘对收盘（spot 取 yfinance daily close，快照日 t 的 spot 仅作参考）。
回测前先把每日快照对齐到交易日历（周末快照归入下一交易日）。

## 四模型增量检验

```
A  price/technical only   （基线：动量、波动）
B  options only           （rr25、pc_oi、term、iv 变化）
C  jev only               （net_index、polarization、bull_share）
D  options + jev          （融合）
```

判定：样本外（rolling OOS）、考虑交易成本后，**D 显著优于 B 且优于 C**，才说明
Jev 叙事对期权指标有增量信息（反之亦然）。任何 D 不优于 B/C 的结果都要接受——
"叙事无增量"也是合法的研究结论。

样本量：7 票 × 60 交易日 ≈ 420 行起步；130+ 交易日才够分训练/测试。

## 已知待办

- 阈值分位化：fv1 的 RR ±2vpt / PC 0.7-1.3 / 叙事 ±0.15 全是绝对猜测，历史够
  长后改为滚动分位
- GEX 口径升级（OI 方向修正、dealer positioning 假设）
- IV rank：需要 IV 历史序列，从每日快照自然累积
- 事件日历对齐：term 驼峰与财报日期对照，区分事件 IV 与真实期限结构
