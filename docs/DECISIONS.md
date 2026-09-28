# 決策紀錄

更新：2026-09-28。本檔濃縮 R001–R015 並列出待決事項。每條決策的完整原文在
`docs/archive/RESEARCH_DECISIONS.md`（R001–R014）與 `docs/archive/REPRODUCTION_SPEC.md`（D01–D11）。
使用者表示（2026-09-28）：「所有內容都可以討論，文檔不是絕對」，因此下列「仍有效」的
意思是「在重新討論前照做」。

## 1. 仍有效的原則與關卡

- **方法變更需使用者確認**（R012 第 7 點）：prompt 語義、模型、embedding、context 截斷／
  摘要／壓縮、檢索門檻、題組與分割、bank 生命週期與更新語義、統計口徑。一般工程修正可自行做。
- **正式比較前停下來確認**（R012 第 8 點）：原規劃是 12 題 × 3 組 × 2 次 = 72 trials。開跑前
  要完整說明設定並取得當次確認；主代理驗收或 pilot 完成都不能代替。規模本身待重新討論（P5）。
- **OpenClaw 零修改**（R013）：只用公開 plugin／hook／設定；不改 OpenClaw 原始碼或 dist，
  不 fork、不 monkeypatch。目前只支援 OpenClaw，Hermes 延後。
- **防洩漏**（D08、R012 第 6 點）：同題（含所有 seed／重複、merge 與 evolve 的祖先）產生的
  skill 不可被該題檢索；hidden verifier 的測試與答案不給 manager 或 solver；每題開始前凍結
  bank，同題所有 trial 結束後才發布更新。
- **三組定義**（D09）：A = no skill；B = extraction only（抽取後直接入庫，不演化、不做模型
  maintenance）；C = full lifecycle（抽取、演化、add／merge／drop）。
- **B／C 對照**（R010）：Extraction-only（B）與 Full lifecycle（C）共用同一份抽取候選，
  不是共用 C 維護後的 bank；maintenance 的檢索不做同源排除。
- **Harness 設定**（D09）：solver `summary_spine=false`，context 270000（250000 輸入 + 20000
  保留），保留 OpenClaw 原生 compaction。
- **注入語義**（R008 經 R012 修正）：
  - Task skill 只在開始時選一次，附加在初始 user 訊息；原生 compaction 後可搬移保留。
  - Event skill 在工具結果之後、下一次決策之前，以 user 訊息注入；compaction 移除原位置後
    不補回，之後遇到相關事件可重新注入；仍在上下文中的同一版本不重複注入。
- **只演化實際注入過的 skill**（R012 第 5 點）。
- **開發呼叫不設總額**（R014）；單次 token、timeout、並行 1 的限制保留。
- **資料邊界**：原始 trace、request／response、ledger、模型快取留在 T2，不進 git。公開
  GitHub repo 只接受 `scripts/create_public_snapshot.py` 產生的脫敏快照。
- **Git 授權**（archive/DELEGATION.md，使用者確認）：可在專案 repo 內建分支／worktree、
  commit，驗收通過後在專案 repo 內合併；不含 push、建立 PR。

## 2. 各決策現況

| ID | 日期 | 內容 | 現況 |
|---|---|---|---|
| R001 | 09-05 | 開發試跑：最多 4 條來源、30 次呼叫 | 已被 R005／R009／R014 取代 |
| R002 | 09-05 | 正式比較前要凍結的內容（題目、設定、門檻、順序） | 原則有效 |
| R003 | 09-05 | 效果解讀：逐題配對差，不把 72 trials 當獨立樣本 | 有效 |
| R004 | 09-05 | TB2.1 commit `7131e437…`；70 題合格；dev = password-recovery、portfolio-optimization；held-out 12 題（mailman、fix-code-vulnerability、prove-plus-comm、feal-differential-cryptanalysis、crack-7z-hash、distribution-search、hf-model-inference、vulnerable-secret、polyglot-rust-c、merge-diff-arc-agi-task、filter-js-from-html、install-windows-3.11）；清單 `evidence/dataset/split-r004-frozen.json`（僅存 T2） | 分割仍在；是否沿用待 P5 |
| R005 | 09-05 | Event 候選要附 trigger／response／outcome step；來源擴充到 10 條文字 baseline；呼叫上限 60 | 證據要求沿用；上限已取消 |
| R006／R007 | 09-06 | 超長 trace 壓力測試的重試與輸出預算 | 歷史 |
| R008 | 09-06 | 每個 trial 一個獨立 proxy，在 request 邊界注入 skill（V05） | 有效，經 R012 修正 |
| R009 | 09-07 | 10 條來源的固定抽取順序；呼叫上限 100 | 歷史（上限已取消） |
| R010 | 09-07 | B／C 共用候選；maintenance 不做同源排除 | 有效 |
| R011 | 09-07 | 配對 prompt 校準一次；引用格式錯誤可修復一次 | 歷史 |
| R012 | 09-09 | 生命週期：task 搬移、event 退休／重注入；每條軌跡最多 3 次 event 抽取、遇 skip 或重複即停；允許多條 event；只演化已注入 skill；方法變更與開跑關卡 | 有效；「最多 3 次」與 R015 實作衝突（見 P10） |
| R013 | 09-10 | 獨立 repo、自帶 OpenClaw plugin、OpenClaw 零修改 | 有效 |
| R014 | 09-10 | 開發呼叫不設總額，保留完整帳本 | 有效 |
| R015 | 09-12～09-27 | 見下方 | **沒有使用者決策紀錄**，下列內容由程式與設定重建 |

### R015（從程式與設定重建，待確認哪些經你核准）

- **C-only 兩輪協定**（`configs/r015-c-only-coding.json`）：只跑 C 組；每輪從空 bank 開始，
  只用同輪 C 軌跡抽取；與 9/4 歷史 baseline 12 題比較。這取代了原本的 A／B／C 三組設計。
- **Task SOP 候選池**：每條軌跡先產生單題 SOP，再做 MiniLM 排序、LLM 配對、合併。
- **多來源逐條引用**：task skill 的每條 rule 都要引用每個來源軌跡的 action 與 result。
- **Code examples**（D04a）：證據綁定、可改寫的 Python／Bash 範例。
- **historical_thinking_policy**：manager 輸入可保留或排除 solver 的 thinking；預設保留。
- **LangGraph Task／Event graph**：以 SQLite checkpoint 支援中斷後續跑。
- **Event graph 每條軌跡最多 8 個 event**。
- **R015 custom prompts**（`prompts/custom/r015_*`）：放寬識別字禁令、改寫選題標準。

### Delegation 規則（archive/DELEGATION.md）

Codex 時期的派工（一位 Terra 實作、主代理驗收）已不適用；派工方式改依全域規則。該檔中的
方法關卡與 git 授權已併入第 1 節。

## 3. 2026-09-28 與使用者討論後的共識

- **修正順序（使用者同意）**：檢索 → 抽取品質 → 免訓練的回饋機制 → RL。
- **先不做 RL**，理由：
  - 目前 skill 送不到對的時機（13 次注入全部無關），RL 無從優化；RL 的執行回饋 R_E 本身也依賴
    檢索正確。
  - 論文 SWE-bench Verified 上，強模型 + prompt 管理與 RL 版增益相當；RL 優勢主要在 TB2。
  - 成本：論文 RL 至少約 3,000 次帶 skill 的 rollout，另加每題 4 次 baseline。本專案 TB2 一次
    rollout 的 agent 階段就要幾分鐘到 37 分鐘（`r015-c-only-formal-20260914-01`），DeepSeek 服務
    一次只能處理一個請求，估計需數百到上千小時。
  - DeepSeek-V4-Flash 無法拿來 RL，只能訓練 4B–9B 小模型，等於放棄強模型 manager 的前提。
- **考慮 RL 的條件**：前三步完成後，檢索精確度與 Fig.14 alignment 都高，通過率仍不動。
  可行版本：DeepSeek 當 teacher 產 SFT 資料訓練 9B，RL 以 rubric reward 為主、少量執行回饋；
  需要專用 GPU。
- **拆 SOP 的理由成立**：Codex 拆成單題 SOP 是為了放進 manager context，論文也必然壓縮過
  （訓練序列 14k），只是沒寫方法。問題在壓縮方式：單題 SOP 會先把各題修法固化。改用精簡
  文字軌跡的提案見 P3。
- **Verifier 要加，分層依序**：見 P4。
- **可以開始離線檢索評測**（使用者 2026-09-28 同意），結果見 `docs/EXPERIMENTS.md` §3。
- **相關性判斷模型（使用者 2026-09-28 指定）**：用 TypeSafe Jev（固定 `jev-1.13.0`），
  不佔 <MODEL_SERVICE_HOST>。離線評測結果見 `docs/EXPERIMENTS.md` §3。是否放進正式檢索路徑仍屬 P2／P4。
- **P2 採用（使用者 2026-09-28 同意）**：
  - 檢索流程：query 修正；MiniLM 預篩（event 0.40、task 0.20）取前 3；Jev Choice 選一條或都不選，
    每階段最多注入 1 條。
  - Jev 失敗處理：
    - 設定錯誤（驗證、格式）直接報錯；
    - 暫時性錯誤重試後仍失敗，記為 `judge_failed`、不注入。
  - Jev 問題措辭第一版（`jev-choice-20260928`）由 Claude 撰寫，未調整過；效果不好時先調這裡。
- **P1 加做維護（使用者 2026-09-28 同意）**：
  - 重新抽取後逐題做 Fig.9 維護，使用論文原版 prompt。
  - 「只抽取」與「維護後」兩份 bank 都評測。
  - 演化需要 solver 軌跡，留到階段 4。
- **P1 做法選 B：另寫精簡 runner（使用者 2026-09-28 同意，尚未實作）**
  - **不走舊 driver 與 SOP 合併**：舊流程是切段、SOP、配對、合併，加上嚴格引用驗證器；這次都不用。
  - **精簡軌跡**（P3）：
    - 題目本文完整保留，SWE 只取 issue 內容；
    - THINK 每段留前 400 字元；
    - ACTION 完整保留；
    - SAY 與 RESULT 超過 1,500 字元時，只留頭尾各 750 字元，並標註省略了多少；
    - RESULT 保留 exit code；
    - 同一步有多個工具呼叫時，用 `ACTION[i]`／`RESULT[i]` 編號對應；
    - 另附官方結果（pass 或 fail）。
    - 以 server tokenizer 實測（SWE codeskill 10 條 + TB2 兩輪 23 條）：中位數約 9.5k token，最大 47k。
      若保留完整 thinking，中位數約 18k，最大 216k。
  - **Event（Fig.7）**：
    - 讀整條精簡軌跡；
    - 每條軌跡最多呼叫 3 次（依 R012，解決 P10）；
    - 第 2、3 次在 user 訊息附上已抽出的 event，要求選不同事件；
    - 遇到 skip 就停。
  - **Task（Fig.6）**：
    - 先用 MiniLM 比對題目本文，取較早的軌跡中最相似的 5 條；
    - 呼叫一次 DeepSeek 做配對判斷。雙方都附題目、官方結果與「指令序列」：
      - 指令序列是依序列出的指令加 exit code，不含輸出；
      - write 的內容截斷；
      - 超過 80 個指令時，只留前 40 與後 40 個；
    - 選出 1–2 條共用同一做法的軌跡後，把這 2–3 條精簡軌跡交給 Fig.6。
  - **Prompt**：
    - `prompts/paper/` 還原為論文原文（commit 0aa2950）；
    - 新版本檔以原文為基礎，含識別字禁令，另加一條規則：
      - Fig.7 第 7 條：when_to_apply 寫成 agent 當下看得到的訊號（錯誤訊息、指令輸出、缺少的工具、
        測試結果、任務情境），不寫修法；必須能只憑最新的指令與輸出判斷條件是否成立。
      - Fig.6 第 6 條：when_to_apply 寫成可觀察的任務情境，不寫解法。
  - **Lint**：
    - 抓檔案路徑、repo 名、反引號內像程式符號的字串、commit hash、長字面值；
    - 反引號外的 snake_case 識別字、`__dunder__`、`名字(` 形式的呼叫也算程式符號，Python 內建函式與全大寫環境變數除外（使用者 2026-09-28 同意；第一次實跑發現模型常不加反引號）；
    - 抓到時呼叫一次修正，標準指令與工具名稱可以保留；
    - 修正後仍不通過，就捨棄並記錄。
  - **Provenance 只記錄、不擋件**：模型回報依據的 step ID，對不上時只記錄。
  - **維護**：逐題做 Fig.9，使用論文原版 prompt。
  - **Code examples**：關閉。
  - **來源與評測**：
    - 來源：SWE codeskill 10 條、TB2 兩輪 23 條，與現有 bank 相同（TB2 共 24 次 trial，round-1 `fix-ocaml-gc` 以 NonZeroAgentExitCodeError 結束、匯入失敗，沒有正規化軌跡）。
      三份 bank 各自從空的開始，依原題序累積；配對只看同一份來源裡較早的軌跡。
    - 產出兩份 bank：「只抽取」與「維護後」。
    - 各自重跑檢索評測與 Jev 評測；新 skill 的標註在看到分數前寫好。
    - 標註除了觸發條件，另寫「建議動作」判斷式，在沒有注入 skill 的軌跡（SWE baseline、TB2 歷史 baseline）上量「觸發後 3 步內 agent 已自己這麼做」的比例；比例高代表 skill 是常規操作（使用者 2026-09-28 同意）。舊 bank 也補標，以便比較。
    - 待看資料後再決定（方法變更）：抽取時優先選 agent 花多步才解決的事件；Fig.10/11 的「超出一般工程常識」子題放進 P4 verifier。
    - 生成端指標（使用者 2026-09-28 同意先做分析）：抽取完成後，用候選的 `evidence_steps` 算「觸發到解決花幾步」，與「agent 本來就會做」比例、人工判讀對照；對得上才討論寫進 Fig.7 提示或做抽取後過濾。讓抽取模型輸出可檢查的觸發／動作判斷式（以 evidence_steps 自我驗證，自動算比例）排到 P4 一起考慮。
    - 「agent 本來就會做」比例當 verifier 的一層：只量相對目前 solver 的邊際價值；觸發次數為 0 時不判斷；怎麼用（降權、門檻或只當診斷）等結果出來再逐步調整，目前傾向高分只降權、不直接擋；最終以 P12/P14 的實際效用統計為準。自動化版本：在無 skill 軌跡的觸發點跑 Fig.14 式判斷。
- **判斷器分工（使用者 2026-09-28 同意）**：
  - Jev：線上、輸入短、要快的決定，例如檢索時的相關性判斷（P2）、日常使用時的即時注入。
  - LLM-as-a-Verifier 評分法（github.com/llm-as-a-verifier，DeepSeek 自評、logprob 期望值、K 次重複）：離線、要讀長證據的品質評分，包括 skill rubric（Fig.10/11）、維護判斷（Fig.12/13）、是否照 skill 做（Fig.14）、日常使用時估計任務成敗（P14）。
  - 同一件事只交給一個判斷器。例外：校準時不需要軌跡的子題兩個都跑，DeepSeek 自評偏誤明顯時改用 Jev Noul。
  - 不用它做 solver 的 Best-of-N，否則量出來的不是 skill 的效果；只能當獨立實驗組。
  - 下一步：P1 抽取完成後，發幾次探測請求，確認 <MODEL_SERVICE_HOST> 的 DeepSeek 能回傳 logprob、能關掉 reasoning（使用者已同意）。
- **Benchmark 分工（使用者 2026-09-28 決定）**：之後的 solver 實驗以 SWE-bench 為主（論文 prompt 管理的增益主要在 SWE；task skill 配對需要同 repo 的相關題）；P1 重抽取仍保留 TB2 一併驗證。
- **P7 時程（使用者 2026-09-28 同意）**：
  - 現在不重構、不刪碼；新工作寫成直接呼叫核心模組的小腳本，不掛進舊 driver。
  - 下一次跑 solver（階段 4）前，寫好精簡 runner（Harbor + sidecar + 抽取 + 發布），
    provenance 只記錄、不當拒收關卡。
  - 舊 driver 凍結，刪除前再問。

## 4. 待決事項（2026-09-28 審查後提出）

依建議順序排列。標「需確認」的屬方法決策，確認前不實作到正式路徑。

| ID | 問題 | 建議 | 需確認 |
|---|---|---|---|
| P1 | 抽取 prompt 恢復論文的識別字禁令（**已同意，方案 B**，見 §3） | 恢復 Fig.6 第 5 條、Fig.7 第 6 條原意；加規則式 lint（函式名、路徑、字面值）；`prompts/paper/` 還原為忠實版本 | 是 |
| P2 | 檢索 query 修正（**已同意並實作**，commit 62c1d4e；新 run 需在 profile 加 `p2_selection` 才啟用，設定見 `configs/p2-selection.json`） | 依離線評測（EXPERIMENTS §3）修訂：task query 用題目本文（SWE 加 repo 名），去掉 harness 樣板與 system prompt；event query 取「錯誤行 + 輸出結尾」、純文字指令、`reasoning_content`，task context 改為題目本文；**不**只在錯誤時觸發（會漏掉 64–85% 相關時機）；skill 端維持全文；門檻重新校準；MiniLM（門檻約 0.40）取前 3 後，由 Jev Choice 選一條或都不選（離線評測：production SWE precision 0.03 → 0.40）；精確度靠這一步 | 是 |
| P3 | Manager 的軌跡表示（**已同意**，隨 P1 方案 B 實作，見 §3） | 改用論文式精簡文字（去 thinking、工具輸出截頭尾），讓 Fig.6 直接讀 2–3 條；SOP 只當配對索引。先用 tokenizer 精確量長度 | 是 |
| P4 | Skill verifier | 依序加入：lint → rubric verifier（Fig.10–13，入庫把關並給修改意見，最多改 1–2 次）→ 檢索時相關性判斷 → Fig.14 alignment 指標。先人工標註 20–30 條現有 skill 校準 judge。Grounding verifier 暫緩；execution verifier 只用於實驗層級比較 | 是（judge 模型與門檻） |
| P5 | 實驗設計 | 同 runtime 成對跑 A／C；樣本要夠大：依既有資料，同條件重跑約 20–30% 的題目會翻轉，24 組配對只看得出約 20 點以上的差距。題組需要「方法相同、bug 不同」的相關題（例如按 repo 分組的 SWE-bench） | 是 |
| P6 | Code examples 延伸 | 凍結（不在正式路徑啟用）或移除 | 是 |
| P7 | 重型 provenance／復原機制 | **時程已同意（§3）**：凍結現有 driver，階段 4 前另寫精簡 runner；provenance 改成記錄，不當拒收關卡 | 刪碼前再問 |
| P8 | Solver 上限實驗 | 對 baseline 失敗題直接注入人工 skill 或同題成功軌跡抽出的 skill（刻意洩漏，只當診斷），量 solver 還有多少改善空間 | 是（要跑 solver） |
| P9 | Agentic manager（pi agent 式多輪修正） | 放在 P1–P4 之後；主要價值是用工具按需讀長軌跡，取代切段／摘要機制 | 是 |
| P10 | Event 抽取次數（**已定：每條最多 3 次**，見 §3 P1） | R012 規定每條最多 3 次；R015 Event graph 預設最多 8 個。需擇一 | 是 |
| P11 | 主力 benchmark（**已定**：solver 以 SWE-bench 為主，P1 重抽取保留 TB2，見 §3） | TB2（12 題差異大、baseline 已解 8/11）或 SWE-bench Verified（可按 repo 取相關題；論文中 prompt 管理在此與 RL 相當） | 是 |
| P12 | 免訓練的回饋機制 | skill 效用統計（注入次數、Fig.14 是否照做、之後成敗；檢索加權、maintenance 淘汰，類似 MemRL）；對照式抽取（同題成功 vs 失敗、no-skill vs 帶 skill）；Best-of-N 由 rubric 挑選 | 是 |
| P13 | RL | 暫不做；啟動條件與可行版本見 §3 | 是 |
| P14 | OpenClaw 日常整合（使用者 2026-09-28 提出的長期目標，之後處理） | repo 要能支援 OpenClaw 這類 agent 的日常使用，不只跑 benchmark：把檢索／注入與抽取包成 OpenClaw 可直接安裝的 plugin 或 hook，一般工作也能累積與使用 skill。與階段 4 精簡 runner 一起設計，benchmark runner 只是其中一種使用方式 | 是（設計先討論） |
