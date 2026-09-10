# 主 agent 首次真 M2 審查

來源：T2 `runs/m2-offline-pilot-20260905-01/`，使用歷史 source，生成式呼叫是本版新實驗。尚無新 solver / verifier 效果結果。

## 首批觀察

- 四個描述有原始引用，可定位來源；task_family 卻多次直接回傳 instance name，未充分表達可重用活動類別。R005 要求一致修正描述 prompt，而非只挑一對重寫。
- fix-git anchor 配對回 no_related_group；它只涵蓋當次候選，不代表十條來源池全部沒有 task group。保留此負結果，不強配。
- call-0006 產生整體 Git recovery 流程，call-0007 真 maintenance add。這證明呼叫與 add 路徑，不證明 event 粒度或 step evidence 合格；首版缺 D04 sidecar。

## D04 補正後人工對照

call-0008／event-d04-corrected.json 引用：reflog 結果 `2c94fcec`；cherry-pick `6e1cfcae`；修改衝突檔 `7057d86a`；stage/continue `20404790`；結果 `92f2ee24`、`faabd02d`。主 agent 已直接查看這些原始工具動作與結果，引用鏈確實存在。

補正後以 reflog 的局部觀察為適用條件，可接受作事件候選的結構與來源證據。仍記錄品質限制：首條規則位於所宣告 trigger 之前；reflog 單獨不足以證明所有分支不可達；discovery/recovery/conflict 多段程序使粒度邊界偏寬。不能由一次來源成功推成一般因果。

決策：不再反覆重抽來追求主 agent 偏好的文字，不手動加入未觀察命令或刪除低品質技能；這些限制保留作診斷。正式初始 B/C 使用一致固定新版流程的共同候選。舊 pilot bank 與新修正分支保持可定位，不冒充同一次原始抽取。

## 尚未通過

Task 真實多軌跡抽取、完整 source pool、超長 fallback、evolve、所有 lifecycle 的必要 evidence、實際 OpenClaw 注入、正式比較及至少十候選人工檢查仍需完成。單一 event/add 不是 M2 或第一版完成。
