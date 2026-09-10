# M3 原生接線受控測試 001

主代理核定：2026-09-09。本文件補充 M3_DEVELOPMENT_PROFILE 的原生 overflow/compaction 前置驗證；不修改 v0.10/R011，不代替後續真 solver/verifier 結果。

- 在隔離 container 使用歷史 Harbor adapter 的 OpenClaw CLI 路徑及同 a28960df22a9b8aed5f37c393c3998b643a18a3d checkout；Node 滿足此 checkout 最低版本。主機 Node v22.22.2 不滿足 >=22.22.3，不能以 host gate 失敗判定功能失敗。
- 本測試 DeepSeek completion 為 0。使用確定性 fake model transport，但執行真 OpenClaw matcher、native compaction、session JSONL 寫入及本版 proxy/detector。
- 最多 8 個 fake HTTP model requests（包括正常、overflow 拒絕、summary、retry），outer wall 至多 900 秒，序列執行；不動 shared OpenClaw checkout 或模型服务。
- 必須保留 request/response、runtime/Node/image版本、真 native session 與 detector refs。禁止手寫 compaction record 代替原生執行，禁止將此結果標真模型解題或 skill 效果。
- 驗收為：標準超額確實導向原生壓縮；新增 compaction 記錄與 before/after request 相符；既有 prior 搬移後可持續；無證據的 anchor 消失仍拒絕。未覆蓋項明列，不能只靠匹配錯誤字串就宣布整個前置通過。
- 通過本受控接線與其他既定前置後，Terra 按原 profile 執行 password-recovery A 後 B 真試跑。若實際模型另需新增前置calls，先報主代理；目前開發 ledger 74/100，不以 fake 測試重設配額。
