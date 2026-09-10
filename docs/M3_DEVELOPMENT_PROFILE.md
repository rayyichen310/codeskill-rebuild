# M3 開發 profile 001

主 agent核定：2026-09-07。依R002固定runtime artifact；不變更v0.10/R011的source校準合約。此文件核定接線與有界開發試跑；必須先完成下列prerequisites，才能啟動官方solver trial。不是M4開跑授權。

## 固定範圍

- 官方TB2.1 commit沿用R004：7131e4375048a0e408a8fb404b5f499d726b695b；固定第一個dev題password-recovery，順序A後B，各1次，非正式72題分母。
- A無skill、B extraction-only；兩組使用相同isolated OpenClaw/Harbor adapter、proxy與量測。各自乾淨container/session/workspace，source不得包含此dev instance。
- 使用R011後凍結的共同候選/B bank；如仍有未解source缺口，要在試跑manifest明列。沒有自然task/event命中就記未觀察，不能為通過而強制注入。
- Solver使用既有DeepSeek-V4-Flash服務，不改服務；記錄實際model/服務metadata、OpenClaw/Harbor/adapter revision及task image digest。無法固定或與前次不同須先報主agent，不以舊metadata替代。

## 執行限制

| 項目 | 值 |
|---|---|
| Spine | false |
| OpenClaw context_tokens | 270000 |
| proxy實際full-payload input | 至多250000，包含tools/template及全部overlay |
| reserve | 20000 |
| 每次max_tokens | 16384，由adapter送出並由proxy檢查不得超額 |
| reasoning | high；保存實際wire配置及server回覆，不將未支援設定冒充已生效 |
| temperature / top_p | 1 / 0.95，兩組相同；屬本版profile選擇 |
| forwarded solver requests | 每trial至多24；實際送出、失敗/重試均留帳，不能重試無限放大配額 |
| Harbor build / agent / verifier | 600 / 900 / 900秒 |
| outer trial wall | 2700秒；保存哪一層timeout |
| proxy upstream timeout | 900秒，仍受較短的remaining trial deadline約束 |
| 並行 | 全部solver/manager模型請求concurrency1，兩trial序列 |

沒有宣称兩次run使用可重現的相同隨機seed；記錄seed是否傳入/服務是否支援。這裡是1個replicate，不是兩個seed。代理步數、tool calls、model requests分開記；不可把24 requests寫成24 agent steps。

## 初始開發檢索設定

- task取最多2，MiniLM cosine threshold 0.45；event取最多1，threshold 0.50。這是待驗證的開發起點，非論文數值、非效果最佳值。
- event沿用最新2個完整tool batch、公開assistant文字與觀察；不把隱藏reasoning作query。所有來源排除、版本去重、bank freeze沿用D05/D08。
- 完整skill累計注入上限25000 tokens（solver input的10%）；不剪掉skill rules以符合預算。記錄exact token算法、tokenizer與每次選取/跳過原因。
- 此A/B pair期間不改threshold。若沒有自然命中，先報未觀察；校準屬後续独立dev profile，不能改跑到一半或用held-out挑門檻。

## 啟動前必要條件

1. 完整solver payload tokenizer：真實tools schema/tool_choice等template欄位納入，與同服務completion usage相等。可做最多2個小型合成probe（每次output至多256、timeout60秒），記入開發100 ledger，不含實際工具執行；若不等則停止，不聲稱exact。
2. OpenClaw overflow matcher：檢查原碼及受控測試，proxy超額的錯誤確實進入既有overflow/compaction處理；任意自訂400文字不足以通過。不能以提高容量/刪訊息替代。
3. Native compaction detector接線：真session事件與proxy request ordinals綁定；沒有證據不能猜anchor消失，合法carried prior可持續後續請求。原生session與真正upstream overlay需同時保存。
4. Adapter確實通過proxy送出16384上限，proxy enforce input/output/request/time限制。三項fixture串流測試不代表真接線完成；要保存actual request、原始SSE/錯誤、usage與official verifier結果。
5. 執行前凍結manifest：Git commit/dirty及完整source snapshot、task/image、兩組設定、bank hash、threshold、時限與順序。原始失敗不得刪除或以重跑成功替代。

以上滿足後Terra可按此profile啟動這2個dev trials，不必再請使用者批准；先向主agent報preflight evidence位置。任何研究方法或profile實質變更由主agent決策。M3只有看到真注入/後續決策/結果鏈才能通過相關條目；本pair本身不涵蓋所有evolve與後題使用驗收。
