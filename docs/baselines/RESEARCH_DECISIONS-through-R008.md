# 研究決策紀錄

本檔由主 agent 維護，補充 REPRODUCTION_SPEC v0.3；不取代其 A01–A19 驗收。每項記錄區分決策與已執行結果。

## R001 — 開發試跑的範圍與預算（2026-09-05，核定、未代表已執行）

- M1 先送一個短推論 probe；M2 使用最多 4 條已授權 baseline source 做描述／抽取 pilot。
- 此階段 manager 請求合計最多 30 次，包含格式修復與重試；每次 max_output_tokens 8192、timeout 300 秒、concurrency 1。記錄實際 tokens、延遲、重試與失敗。
- 如果輸出或呼叫預算不足，保留原始失敗並回報主 agent。不可靜默截斷、把失敗改寫為 skip 或自行擴大試跑。
- 這是有限的工程開發 profile；M3 live 任務與 M4 正式 72 trials 仍須先固定各自設定、題組與成本上限。
- 不要求模型為了驗收硬湊相關 task group，亦不要求自然產生每一種 maintenance 分支。未觀察分支以 fixture 驗證功能並明示 live 未觀察；是否滿足 M2 由實際 evidence 判斷。

理由：先驗證模型與新程式的真實協作路徑，再設定正式預算。此決策不降低完成條件，pilot 不計入 M4 分母。

## R002 — 正式比較前必須固定的內容（2026-09-05，核對要求）

Terra 提供候選 profile，主 agent 核定後另存不可變 manifest：

1. Source、dev、held-out instance IDs、dataset 與 task hashes；source/dev 與 held-out 不重疊，跨 seed/arm 同題身分一致。
2. 相容任務集合的建立方式與排除原因；以安裝／平台条件決定資格，不根據解題成敗或技能匹配程度選題。抽樣與任務順序的種子必須在結果前固定。
3. Solver 與 manager 版本、tokenizer/template 指紋、temperature、seed 實際支援情況、context/output budget、timeout、steps/model-call limits、重試規則、總預算及中止規則。
4. Task/event 檢索門檻與注入預算；門檻只能使用 source/dev 校準。不同 seed 的重複試驗若服務不支援可控 sampling seed，必須標為 replicate ID，不能宣稱相同隨機數控制。
5. 三 arm 的執行順序與服務版本檢查；每一 instance 完成全部 arm/replicate 後才更新可供下一題使用的 bank。任何中途版本漂移要標示，不能默默合併為同一固定模型比較。

主 agent 核對完成前，不開 M4 批次。這是內部研究決策點，不需要使用者重複批准既有授權。

## R003 — 效果解讀邊界（2026-09-05，核定）

M4 的 online bank 隨已完成題目更新，因此後題結果依賴前題經驗；兩個 replicate 也不是獨立新任務。主結果先呈現固定題序下的逐題配對差、完整成功／失敗分母及成本，稱為此小樣本 online protocol 的觀察。

不可直接把 72 trials 當成 72 個獨立樣本計算一般成功率顯著性。即使以 instance 聚類，單一 online 題序仍有跨題依賴；若日後需要穩健不確定度或一般化效果，應另設獨立 bank／題序重複或 frozen-bank 實驗，不在本版偷偷改成其他 protocol。

「注入成功」、「軌跡顯示遵循」與「官方 verifier 改善」分別報告；負結果或差異小也可完成既定比較，不能據此降低工程验收門檻。

## R004 — 官方資料發現與分割（2026-09-05，核定、清單待產生）

目前未找到完整本機官方 task checkout，故允許將官方 Harbor registry `terminal-bench/terminal-bench-2-1` 的 metadata/task packages 下載至本專案 cache，必要時使用官方 GitHub repo；保存 resolved revision/digest、任務檔 hash 與時間。這是唯讀取得任務資料，不等同執行 benchmark。

- 先列 task IDs、task.toml、環境需求與檔案 hash。不得閱讀 solution 或 hidden verifier 內容來選題；不得以預計解題成敗、技能是否匹配或難度篩選。
- 靜態相容條件：現有 Docker/Linux 可支援、不需新增 GPU 配置／私人憑證／額外特權；尚未支援的圖片輸入需列排除原因。不得先 pull 全量 Docker images 或試跑 solver 才選題。
- 排除昨日全部 12 個 instance（包括 Spine、code-from-image 與 setup failure）；即使其中資料尚未進 source bank，也不放入 held-out。
- 對剩餘 eligible canonical IDs，計算 UTF-8 字串 `codeskill-v1-20260905-split\n` + ID 的 SHA-256，其中 `\n` 為一個換行字元。依 digest 十六進位字典序排序，同分以 ID 排序；前 2 個作 dev，接下 12 個作 held-out，其餘記為未使用 reserve。
- Terra 提交完整資格清單、理由與 hashes，主 agent 核對後凍結。少於 14 個則回報。後續安裝失敗保留為 infra，不以 reserve 默默替換；任何必要變更先記錄並判斷是否須重啟 protocol。

來源：[官方執行文件](https://www.tbench.ai/docs/run-terminal-bench-2-1)、[官方資料 repo](https://github.com/harbor-framework/terminal-bench-2-1)、[Harbor registry 說明](https://www.harborframework.com/docs/run-jobs/run-evals)。此分割方法是我們的決定，並非 CODESKILL 論文原設定。

### R004 執行紀錄：分割已固定，執行 profile 尚未固定

官方 commit `7131e4375048a0e408a8fb404b5f499d726b695b`；89 題靜態盤點、70 題符合本版範圍。排除昨日 12 題及 7 個額外以視覺/OCR/影片輸入為主且尚未驗證多模態軌跡處理的任務；排除有逐項理由，不宣稱文字 agent 一定無法藉工具解題。產生圖像或處理數值模型本身不等於需要把圖片注入模型。

- Dev：password-recovery、portfolio-optimization。
- Held-out 固定順序：mailman、fix-code-vulnerability、prove-plus-comm、feal-differential-cryptanalysis、crack-7z-hash、distribution-search、hf-model-inference、vulnerable-secret、polyglot-rust-c、merge-diff-arc-agi-task、filter-js-from-html、install-windows-3.11。
- Canonical ID 使用 task folder basename；官方 `terminal-bench/name` 與歷史來源須映射至同一 ID。
- 完整清單：T2 `evidence/dataset/split-r004-frozen.json`，SHA-256 `7650eeced6fdce5f07cc23647e7713310a4769c7a204ac9aade4d5278ef8fddc`，保存 task tree OID、task.toml 與環境檔 hash。

只有 task split 固定；Docker image digest、solver/manager profile、檢索門檻與實際安裝/live 閉環仍待驗證。此清單不能單獨作為 M4 開跑或通過依據。

## R005 — 首次 M2 結果後的開發修正與來源擴展（核定）

首批 4 條來源產生 4 個描述；fix-git anchor 的配對回覆 no_related_group。Event 抽取與 maintenance add 真實執行，但主 agent 發現候選覆蓋整題工作流程，且缺少 D04 要求的 trigger/response/outcome step sidecar。這些結果保留，不標成 event 品質通過；no_related_group 也不代表整個 10 條來源池都沒有可用組合。

- 在 runtime/custom prompt 補齊原本 D04 的證據欄位與局部事件要求，保留 paper prompt 原文及 delta；不手寫技能、不強迫 generate，不將舊輸出補寫成新模型證據。
- D01 task_family 應是可重用的活動類型，而非直接複製 instance ID。若修改描述 prompt，對本次來源一致重新描述，版本與原始結果都保存，避免只修理不利的配對。
- 允許擴展至既定 10 條文字 baseline 初始來源，均為既有已授權歷史資料，不重跑 source。這是完成本已規劃來源池，沒有增加正式題數或接觸 held-out 結果。
- 開發 generation calls 總上限由 R001 的 30 調整為 60，累計包含先前 2 probes、首批 7 calls、修復、正常與 stress calls；每次 output 8192、timeout 300 秒、concurrency 1 不變。這不是正式 M4 的預算。
- 四來源 no_related_group 保留；完整池仍無適當組合時回報，不把單軌跡當 task 抽取或降低相關性要求。先跑配對與抽取再看證據，不預設一定有 task skill。
- D03 壓力測試用獨立 run：對已實測約 19,954 input tokens 的 event request 設 development context 24,000（8192 output + 4096 margin，input allowance 11,712），按 action-observation 邊界分段、真實 step-ID summaries、再最終抽取，初始最多 4 個額外 calls。它只測超長處理機制，不代表實際服務塞不下，也不支持壓縮提升品質的結論。
- Pilot bank 和舊 prompt 結果保留為開發 evidence；正式初始 B/C bank 必須使用同一固定新版流程、一致生成的共同候選。不得手動只刪低分候選來美化正式效果。

對應 A02/A03/A04/A18。此修正發生於任何新 development/held-out solver 結果之前，正式評估分割保持不變。

## R006 — 超長壓力測試第二次嘗試（2026-09-06，核定）

第一次 stress 保留為未完成：3 個摘要請求已執行；摘要引用涵蓋 26 個來源 entry，若將所有引用都還原為完整原文，最後 input 21,090 超過 11,712，故程式正確停止，沒有最終抽取。這不能標成 fallback 通過。

核定獨立新 run 最多 3 個额外 completions（最多 2 個摘要 + 1 個最終抽取），仍計入 R005 的累計 60 上限，不重寫第一次 evidence：

- Final development context 24,000、output 8192、margin 4096，allowance 11,712 保持不變；摘要請求的 output profile 可較小，但需完整保存，且以同服務 exact counter 核對。
- Segment packing target 可由 10k 提至 12k，前提是完整 request 仍低於摘要階段已核定 allowance 17,856；保留完整 tool batch，不能拆斷動作與回覆。
- 分開 `covered_step_ids`（摘要概況涵蓋的來源）和 `verbatim_evidence_step_ids`（最終抽取帶回原文的局部證據）。不能因概況引用某步，就把所有原文全部塞回；亦不能把未带回原文的步驟誤稱完全未處理。
- 每段至多選 3 個完整 action-observation pairs 作原文證據，摘要仍須涵蓋該段程序與結果；最終保留完整任務背景、全程概況、候選事件的前因／反應／後果，以及原始最終結果。引文選擇由模型依觀察證據決定，不手寫想要的事件。
- 以實際 tokens 為最終邊界。若限制導致必要事件證據不完整，或最後仍放不下，保持 context_blocked 並回報，不能任意剪掉關鍵結果來湊通過。

這是壓力測試與壓縮路徑的工程修正，既不提高服務 context，也不證明壓縮品質優於 full。正常 full path、正式 task split 與 A01–A19 不變。

## R007 — 壓力測試輸出預算與工程自主範圍（2026-09-06，核定）

第二次 stress 的第二段摘要耗盡 2048 output tokens，其中 2045 為 reasoning，JSON 不完整，已正確標 truncated 並停止。這是過小的摘要輸出 profile，不是證明來源不可壓縮，也不是 skip。

- Stress 累計 generation 上限改為 12（包含第一次 3、第二次 2，全部仍包含於 R005 總 60）；在此上限內，Terra 可自行處理一般輸出不足、有效 checkpoint 復用及一次格式修復，不必逐次再請主 agent 批准。
- 摘要 max_output_tokens 可在 R005 上限 8192 內調整；建議本次採 8192，reasoning 與可見輸出合計使用此預算。每次仍 exact preflight，重新扣除實際 output reserve 和 margin，不能沿用舊 allowance。
- 原 stress final context 24,000、output 8192、margin 4096 保持。當前兩段約 9,894／10,609 input，若重用原段請求、提高 output，仍須逐次 exact 確認可用 11,712。
- 可在新 run 引用之前有效第一段摘要作 checkpoint，保存來源 request/response hash及當時profile，不修改其原始失敗 run，也不需重跑已成立的相同摘要只為整齊。
- 若在完成無損欄位 projection 後改用較小的新 stress cap，先保存新 full-input 基線和版本，將它作不同 stress scenario，不能覆寫前兩次測試。必要時回報主 agent 核定新 scenario；不為通過而改丟失證據或排除來源。

工程自主不包含改變論文語義、正式分割、來源排除、證據完整性或降低驗收要求。超出上述總額或需要改方法時仍回報主 agent。

## R008 — OpenClaw request-boundary overlay（2026-09-06，核定）

本機 OpenClaw 的 after_tool_call 是 fire-and-forget；next-turn injection 每個外層 run 只 drain 一次，無法保證每批工具回覆後、下一決策前的即時注入。主 agent 及 Terra 均已從現有原碼確認。

選用本專案獨立、每 trial 隔離的 OpenAI-compatible proxy，作 V05 明示整合變體；不改共享 OpenClaw checkout。三 arm 都經相同 proxy 與量測／預算機制，A 不改寫模型訊息。

1. Task skill 只選取一次，附加至最初 task user message；後續重送歷史保持同一 block，不變成 system 指令。
2. Event 檢索在完整 tool-call batch 回覆後同步完成，再轉送下一個 solver request。以 tool_call_ids / 對應結果為 anchor，記錄選中 skill ID+version、當時 bank snapshot 與實際 block。
3. 後續 request 依原 anchor 位置重新建立相同 supplementary user messages，保留其時間位置。這是把 OpenClaw 原生未保存的 overlay 還原到實際模型歷史，不是每輪新增同一技能，也不能把所有 event skills 每次搬到 system 或 initial prompt。
4. Native session 與實際 upstream prompt 分開保存。新 trajectory 的 skill-conditioned 證據由原始 session 加 proxy injection/request records 對照，不能聲稱原生 session 本身包含這些額外訊息。
5. 原生 compaction 保留並記錄。若 compaction 使舊 anchor 消失，原 skill block 以明示的 carried-prior user block 保留在 compacted history 起點，保存原 anchor 與 relocation 原因；此分支是 V05 額外語義，需獨立測試，結果分層報告。不能猜測 anchor 或默默遺失技能。
6. 使用 exact counter 計算真正轉送的 input，包含所有 overlay。所有 arm 遵守同一 250000 input / 20000 reserve 設定；超額時回傳明確 context error，讓既有 OpenClaw overflow/compaction 機制處理並留 evidence，不靜默刪歷史或放寬容量。若它無法恢復，分類為 context/infra 邊界並回報，不宣稱已解決。
7. Skill 總 budget、來源排除、同一版本只選一次、frozen bank 與 paper/runtime prompts 沿用原決策。Proxy 不修改 solver 輸出、tool result 或 verifier 結果；streaming 與失敗需完整留證。

必要驗證：A input pass-through；初始及 event 真 upstream 訊息位置；多工具批次；至少兩次後續請求仍保留同一 block且不重複新增；跨 trial 隔離；實際 token 邊界；compaction anchor relocation 分支。Fixture 不取代 A10/A11 的真 OpenClaw 試驗。這是方法選擇，工程細節由 Terra 實作與 debug。
