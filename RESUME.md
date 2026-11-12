# RESUME · data-agent 断点档案

> 状态：**2026-11-12 02:20 全部完成，任务关闭**。留此档仅作教训存证。

## 教训存证（续作时犯的两次误判）

1. toml `main="index.js"` 被我判「退化」改回 `worker/index.js`——**错**。wrangler
   从 worker/ 目录内部署，相对路径正确；我改回导致 entry-point 报错。
2. `compatibility_date="2026-11-12"` 超 CF 支持上限报 10021——前会话写
   `2025-01-01` 是被这个错教育过的正确值，我又「修」回错的方向。

**规矩：对前人 WIP 的「看起来不对」，先部署实测再判，判错要落痕。**

## 最终完成态

1. [x] GitHub：duskly-jpg/data-agent，最终提交 1c8bc0c，CI 绿（含 D1 拆视图修复、
   AI 响应解析加固、兼容日期回退）
2. [x] D1：8000 用户 / 2710 订单 / 35899 事件 + 修复版拆视图（前会话灌好，验证过）
3. [x] R2：3 个 CSV 血缘层（seed/ 前缀，回读验证）
4. [x] Worker 部署 + zone route 子域压制（部署三连击纪律：cd 绝对路径原子整条）
5. [x] 端到端：主问句 AI 路径出 SQL+结论（留存 39.05% 含观察窗口径说明）；
   线上负样本 DROP/DELETE/越权/臆造指标 四类现场拦截；blocked_queries=6 累计
6. [x] 模型修复：llama-3.1-8b 已弃用（5028, 2026-05-30）→ fp8-fast（wall 同款
   实测）；解析加固：非 string text 强转
7. [x] 交付：TA 令——完成通知不输出敏感文案，已最小化通知

## 红线（常驻）

- 密钥不进仓不进对话；wrangler 必须套 secretctl run（否则 token 不注入、静默失败）
- 每次中断前：更新本文件 + daily 落一行
