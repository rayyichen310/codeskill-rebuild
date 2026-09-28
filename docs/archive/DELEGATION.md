# 第一版執作與研究決策分工

## R014 當前工作（使用者已授權）

開發模型呼叫總額不限，既有帳本保留；單次保護、R012 每軌跡三次 initial 探索規則與正式 72 trials 關卡不變。由一位 Terra/high 補長 trace 自動 fallback、含 compact 歷史證據及 unlimited 帳本支援，主代理同時完成 A01–A19 證據盤點。離線穩定交付後，主代理驗收並安排真實開發驗證；不能自行修改尚未核定的方法參數。OpenClaw 原始碼零修改與禁止巢狀派工繼續適用。

更新：2026-09-09，對應 REPRODUCTION_SPEC v0.11／R012。使用者確認的方法變更與正式開跑關卡，取代舊有「只需主代理批准」的說明。A01–A19 仍須完整驗收。

## 主 agent

- 負責整體規劃、論文解讀、方法與實驗設計、可信度判斷及後續工作決策。
- 擁有 REPRODUCTION_SPEC.md、docs/baselines/、研究決策紀錄與最終验收的修改權。
- 根據原始檔案、測試、model calls、prompt 注入及 official verifier 自行核對 Terra 的結果；不以 Terra 自報或 judge 分數代替驗收。
- 修訂 R012 的方法決策前，先向使用者說明證據、理由、影響與必要重驗，等待確認；Terra 提出的建議也適用。確認後才更新契約版本；符合已同意行為的一般工程修正可繼續。

## 實作代理

- 數量 1，模型 gpt-5.6-terra，effort xhigh；繼承 workspace-write sandbox 及現有 approval policy，不能放寬權限。
- 主要寫入範圍：T2 `<PROJECT_ROOT>/`；如需本機編寫／傳輸，使用獨立 `<LOCAL_PATH>`，避免與主 agent 文件副本衝突。
- 使用 SSH 專用 config；可讀既有模型及已授權歷史 trace / OpenClaw runner 設定。可在新專案建立獨立依賴環境、撰寫程式、測試及執行規劃內模型與 benchmark 請求。
- 網路限本任務必要的 T2/<MODEL_SERVICE_HOST> 連線、既有 DeepSeek endpoint、官方依賴與 benchmark 資源。不得部署、重啟或修改共享模型服務，不改全域設定，不清理舊實驗或其他工作目錄。
- 不修改舊 CODESKILL、不 import 舊核心，不沿用舊 bank / skills / 測試結果。允許參考昨天 OpenClaw+TB2 設定和使用白名單 raw sessions。
- 一般工程選擇、debug、重跑受影響 suite 可自行處理；不得建立、spawn、委派或要求任何其他 subagent。
- 不自行啟用 Sol；遇到難題只回報主 agent，附具體失敗、已嘗試方法和建議。

## 執行與回報

1. 開始前讀取現行 REPRODUCTION_SPEC、RESEARCH_DECISIONS 的 R012、DELEGATION、STATUS、TRACE_REUSE、CONNECTIONS 與適用規則。交接時明確確認文件版本與停止關卡；不能只依賴檔案存在。
2. M1：核對版本、服務、tokenizer/context、harness 和歷史 raw sessions。提出候選 run profile（timeout、steps、token/call 總預算、seed/task manifests）。
3. M2：完成新核心、importer、真 MiniLM、DeepSeek manager、版本化 bank、provenance、context 處理及必要測試；使用既有 baseline raw trace 做真實離線 manager calls。
4. M3：依核定 profile 跑真實 OpenClaw/官方 TB2 的 skill 注入與更新閉環，保存 raw evidence。
5. M4：正式 12 題 × 3 組 × 2 次（共 72 次）試驗前必須停止。主代理向使用者完整說明所有 CODESKILL 功能、實際驗證、限制與凍結設定，取得明確確認才可開跑。主代理自行驗收或之前的概括派工授權，都不能取代此次確認。
6. 每階段回報已做／未做、精確命令、測試斷言、evidence paths/hash、失敗及分類。允許負結果；不能靠縮小範圍、換題、增 timeout、偽造操作分支或挑 seed 製造完成。
7. Terra 維護 `docs/IMPLEMENTATION_STATUS.md`、工程說明及 issues evidence；主 agent 維護正式 STATUS 與企畫書。避免雙方同時改同一文件。

## 必須回報主 agent 的方法問題

- 來源軌跡無法形成 2–3 條相關 task group、資料洩漏或來源分割衝突。
- 更換模型、embedding、agent 行為、prompt 語義、context 截斷／摘要規則、retrieval 門檻策略或 lifecycle 更新語義。
- 原文無法合理對齊、需要改規格或驗收門檻，或模型不能提供規劃所需輸入／操作。
- 正式 held-out 開始後的任何參數或題組修改、偏離公平對照、效果宣稱與統計口徑問題。

方法尚待決定時，凍結受影響路徑，由主代理與使用者討論；其他符合已同意行為、互不依賴的一般工程工作可繼續。不能只憑主代理或代理間同意就採用新方法。

## 主 agent 驗收方式

- 親自開檔審查 importer、bank 更新與來源排除、檢索注入、計量／比較實作。
- 親自執行完整受影響 suite，抽查真實 model calls、source steps、prompt injection、官方結果及所有試驗分母。
- 把 offline、fixture、live、historical、reconstructed 證據分開；按 A01–A19 回填 acceptance。
- 只在各項證據實際成立時提升里程碑狀態。

## 語言與高效率交付（2026-09-09）

- 保留一位 gpt-5.6-terra 實作代理、effort xhigh，維持原 workspace-write 與網路範圍。本文件不啟動或恢復代理；不得自行降低 effort、追加代理或擴張權限。
- 要求實作代理使用英文推理，程式碼、註解及向主代理的工程回報使用英文。主代理與使用者以繁體中文溝通。依使用者最新要求，企畫書、派工說明、研究決策、進度與其他供使用者閱讀的專案文件以繁體中文撰寫，保留必要英文術語；不將工程文件預設英文的規則套用到這些文件。
- 依已確認需求與驗收斷言，交辦邊界清楚的完整功能。Terra 自行完成一般實作、debug 與測試循環，在功能完成、重大阻礙或方法決策點回報；不反覆詢問未變的狀態。
- 驗收節奏依工作性質區分：需求清楚、範圍明確的實作，由 Terra 完成一個穩定功能階段後集中驗收；方法不明或影響範圍大的決策，應在採用前中途回報，不等到整批實作完成才提出。中途回報不代表主代理持續盯碼；涉及方法變更或需求偏離時，仍依既有規則交由使用者確認。
- 交接包含改動內容、驗收結果、未解問題及精確證據路徑／hash。完整 log 留在檔案，按需讀取相關片段；不在每次交接重複提供整段對話。
- 主代理獨立閱讀關鍵差異與原始證據，執行必要的受影響檢查。同一版本若無新疑點，不反覆重驗；修正後仍須重跑完整受影響測試，並保留首次失敗。節省用量不取代獨立驗收或使用者確認關卡。
- 不為重述本文件另建 tracker 或獨立規劃文件。禁止巢狀派工：不得建立、spawn、委派或要求任何其他 subagent。


## Git 分支、提交與合併流程（使用者已確認）

本節適用於 `codeskill-rebuild-20260905` 專案。使用者已授權為此工作建立分支／worktree、暫存明確列出的專案檔案、建立 commit，以及主代理驗收通過後在專案倉庫內合併；不需逐次重問相同授權。此授權不包含 push、建立 PR、修改其他倉庫或正式 72 次試驗；既有方法變更與正式開跑確認關卡仍有效。

1. **先固定基準。**導入流程時先盤點既有未提交修改，區分原有工作、本輪實作與文件；保存必要備份並記錄基準 commit、目標整合分支及檔案歸屬。只暫存核對過的明確路徑，不用整包暫存混入無關變更。基準快照不代表其中功能已通過驗收，缺少歷史版本的部分必須明說。
2. **在獨立分支實作。**Terra 使用 `codex/` 前綴的功能分支，優先搭配獨立 worktree，從記錄的基準開始。既有進行中工作在穩定交付點銜接，不能為切換流程丟棄或覆寫未提交修改；導入完成後不直接在整合分支實作。
3. **以完整功能階段提交。**Terra 自行完成實作、debug 與受影響測試，留下可讀的 commit；交接提供 base/head commit、差異摘要、測試證據與未完成項目。原始大型實驗產物以受控證據路徑與 hash 引用，不把憑證或無關產物納入提交。
4. **主代理集中審核固定版本。**以核准需求及明確的 base/head 差異驗收，親自檢查關鍵修改與原始證據，執行完整受影響測試。Commit 不是驗收通過的替代證明。需要修正時交回 Terra 追加修正 commit，保留審查歷史；head 改變後，先前通過不能自動涵蓋新修改。
5. **通過才合併。**主代理確認被審核的 head、驗收結果及整合分支狀態後，才在專案倉庫內合併。整合若造成衝突或新修改，修正後重新驗證受影響行為，再宣告完成；未完成或未通過的功能留在分支，不把已合併等同於真實閉環或正式試驗通過。
6. **維持階段性交付節奏。**需求清楚的工作等穩定 commit 交付後集中審核；方法不明、重大阻礙或影響範圍大的決策在採用前回報。沒有新版本或新疑點時，不重複輪詢或審核。

## R013 派工硬性邊界（2026-09-10）

使用者授權同一 Terra xhigh 研究後實作獨立 CODESKILL repo 的 OpenClaw 接線。只修改 CODESKILL 程式、plugin、文件及測試；不修改任何 OpenClaw 原始碼或 dist，不採 fork／monkeypatch，不做 Hermes。取消先前 purpose-marker 上游修改方案。主代理按 R013、R012 與 A01–A19 驗收；取消／失敗後的一般請求不得繞過 skill overlay。現行 87bebfc 未通過完整驗收，待 Terra 修正。不得建立、spawn、委派或要求任何其他 subagent。

## Terra effort 動態調整授權（2026-09-10）

使用者明確授權主代理依任務難度調整 Terra 思考等級，不必逐次再問。預設 high；複雜時序、根因不明或反覆修正失敗時可用 xhigh，釐清後回到 high。現有工具無法直接修改執行中代理的 effort，因此在穩定交接點以相同模型的接續代理切換，保留原任務、證據和進度，同時只維持一位 Terra 執行。此授權不放寬零 OpenClaw 修改、禁止巢狀派工、實驗方法與正式試驗關卡。
