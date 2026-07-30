# 数据契约

## 最小输入

| 数据集 | 必需字段 | 推荐字段 |
|---|---|---|
| 订单 | `order_id`, `customer_id`, `order_date`, `currency`, `order_amount` | `sku`, `quantity`, `unit_price`, `salesperson`, `country`, `incoterm` |
| 发票 | `invoice_id`, `order_id`, `invoice_date`, `currency`, `invoice_amount`, `due_date` | `tax_amount`, `invoice_status` |
| 收款 | `receipt_id`, `receipt_date`, `currency`, `receipt_amount` | `invoice_id`, `order_id`, `bank_reference`, `bank_fee` |
| 成本 | `cost_id`, `cost_date`, `currency`, `cost_amount`, `cost_type` | `order_id`, `sku`, `supplier_id` |
| 汇率 | `from_currency`, `to_currency`, `rate`, `rate_date` | `rate_type`, `source`, `version` |

出货、费用预算和银行流水不是所有分析都必需，但缺少它们时必须声明对账或归因范围受限。

## 标准化规则

- 日期使用 ISO `YYYY-MM-DD`；时间保留时区。
- 币种使用 ISO 4217 大写代码。
- 金额同时保留 `original_amount`、`currency`、`fx_rate`、`fx_rate_date`、`reporting_amount`。
- 汇率方向固定为：`1 from_currency = rate × to_currency`。
- 金额精度遵循企业会计政策；中间计算保留足够精度，最终展示再舍入。
- 退款、折让和冲销使用明确类型与符号规则，不根据负号自行猜测业务含义。

## 关联优先级

1. 明确外键或业务单据号。
2. 经确认的映射表。
3. 复合键（例如客户、币种、金额、日期窗口）只能生成候选匹配，必须人工确认。

## 分析前确认清单

- 分析期间与数据截止时间
- 报告币种与汇率类型（交易日、月末、月均或企业内部牌价）
- 收入确认时点
- 含税/未税与运费、关税的归属规则
- 退款、折扣、佣金和银行手续费口径
- 预算版本
- 客户账期与逾期定义
- 是否允许使用预测及其决策范围

