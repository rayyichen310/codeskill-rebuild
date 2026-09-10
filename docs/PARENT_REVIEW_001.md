# 主 agent 首批核心審查（2026-09-05）

範圍：Terra 首批 staging `src/codeskill_rebuild/`。本輪是開發中審查，未完成模組不當成最終成果；以下問題已交 Terra 修正，未回驗前不得標 pass。

## 已執行反例：bank freeze 與 hash

對空庫 add 一個來自 s1 的 event skill，保存 snapshot，再以 s2 merge 該技能。主 agent 使用 Python 直接呼叫公開 API，得到：

```text
recorded_after_matches_current False
frozen_snapshot_length 2 expected 1
frozen_snapshot_old_status superseded expected active
historical_eligible_count 0 expected 1
```

對應缺陷：snapshot 共用可變容器；after hash 計算時 journal 尚未追加；sequence 邊界不能恢復後來被 supersede 的舊版本。要求 immutable snapshot、明確 state/journal hash 語義及對應回歸測試。對應 A09/A12。

## 靜態檢查要求

- bank add/evolve/merge 必須聯集完整祖先；evolve 後 add 或 merge 到另一 target 不可遺失被修訂技能來源。
- context 任意重複行去重會刪動作、錯誤與驗證。僅允許可辨認的無資訊進度重複；未實作前禁用。UTF-8 bytes/3 不是 token 上界，長軌跡須真 tokenizer/template。
- manager 必須保存 timeout/invalid/context failure；拒收 finish_reason=length，即使殘留字串碰巧可解析 JSON。30 次 pilot 預算須涵蓋 probe、修復與重啟。
- importer 核對 message 層 stopReason/usage；不能以 dict 覆寫重複 IDs 掩蓋問題，需檢查 parent/tool pairing。抽取軌跡要包含中途 user 訊息與控制／缺失紀錄。
- importer 不可用泛化 secret/token/password 正則任意抹去任務程式碼或合成憑證，然後稱完整證據；必要 redaction 須分類與定位。圖片 pending 必須阻止以文字完整軌跡抽取。
- MiniLM 配額需按真 tokenizer 的 wordpieces 與特殊 tokens 計算；先按 words 裁切再讓 encoder 截尾不能保证 rules 或最新事件保留。測試應查看實際编码輸入，而不是只看字符串有欄位名。
- event user 訊息應等同批工具全部回覆後再插入下一次 decision；首批已包含這個防護，仍待真 OpenClaw hook 驗證。

已執行反例與靜態判定分開；Terra 自報修復後，主 agent 再開檔及重跑受影響驗證。
