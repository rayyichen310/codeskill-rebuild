# 研究決策紀錄

本檔由主 agent 維護，補充 REPRODUCTION_SPEC（目前v0.10）；不取代其 A01–A19 驗收。每項記錄區分決策與已執行結果。

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

## R009 — 完整來源的一致共同候選流程（2026-09-07，核定）

10條baseline描述已完成。主 agent核對10份source hash、引用ID存在性、HTTP200/stop及exact preflight對usage一致，最大input161609。這只支持描述transport及來源可追溯，未驗證全部敘述真實性、skill品質或效果。kv-store-grpc的描述只報局部自測成功，而官方reward為0；不能混稱正式成功，後續保持原outcome。

在任何本輪配對/抽取結果出現前固定：

1. 以canonical instance ID字典序遍歷全部10個anchor，每個使用固定MiniLM對其餘9份描述排序（原上限12不變）；交DeepSeek選含anchor的2–3條不同題軌跡，允許no_related_group。
2. 同一排序後source ID集合只執行一次task抽取；保留重複group的所有原配對決策及去重原因，不依預期效果挑group。task抽取收到完整投影軌跡；超額走既定D03且保留context標記，不能減成單條。
3. 完成task階段後，按同一10個source順序，每條首輪固定一次event抽取，可skip。D04的至多3次是上限，不是必須湊3個；本次共同初始bank使用一致的一次首輪，不依某條結果優劣追加抽取。後續要增加一致輪次須另記決策。
4. 每個生成候選立即經真maintenance，按上述固定生成順序建立一份共同bank，作B/C共同初始來源。不得手改skill、挑掉差候選或以always-add取代模型決策。skip、無效格式、context_blocked與API錯誤各自保留。
5. 每個candidate保留原始來源/證據及完整祖先；描述用途只限搜尋配對，不把局部成功或agent自述當官方reward。保持失敗來源，不因reward0刪題。

開發generation累計上限從60調整為100：當前28已用，最壞10次配對+10次task及10次maintenance+10次event及10次maintenance=50，另留22次給既定修復、context與開發evolve。stress仍累計12且包含於100；output8192、timeout300秒、concurrency1不變。這不授權M4正式批次；M3 solver profile需主 agent核定。預算未用不強制消耗，超額仍回報。

全池仍無適當task group時如實回報，A01不能用單軌跡或fixture冒充live通過。現有成功描述不重跑：其projection修正版當時仍名v1，保存執行code hash sidecar；後續升v2並保存code identity，不覆寫stress原始證據。來源、R004分割與A01–A19均不降低。

## R010 — 共同候選與維護檢索的合約澄清（2026-09-07，核定）

主 agent在檢查R009 runner時發現兩點，發生於任何新solver trial之前：

- 主 agent在R009第4點將共同bank描述為B/C初始來源，措辭不精確；原D09明定B為Extraction Only，不經模型maintenance。正確共享的是抽取候選清單。此項由主 agent負責修正，不能讓兩arm共同使用C維護後bank而改變對照。
- Runner的maintenance_retrieval排除與candidate來源有交集的舊skill。D07只規定同類相似候選，P11/附錄C的同題排除為evaluation防洩漏。Figure9描述合併相同capability、消除重複；沒有在該prompt規定同源排除。將同源排除用到maintenance會阻止合法去重，是本版需撤除的實作選擇。

核定處理：

1. R009按固定順序產生的合格候選原文、skip與錯誤全部保留；不重抽求較好候選。B/C引用同一份候選manifest。
2. B直接保存全部schema合格且非完全重複的候選；不呼叫maintenance/evolve。完全重複依canonical skill內容判定，忽略run等附加metadata；合併重複來源時仍聯集provenance，不得因去重而遺失任一source ID。此操作標為deterministic extraction-only ingestion，不假裝模型add/merge。
3. C依固定候選順序真maintenance，檢索同benchmark/granularity的active skill，MiniLM top5，不做candidate-source交集排除。保留完整來源祖先；當solver取用時，D08的同題、跨seed、未來資料排除完整照做。
4. 已啟動的舊R009 run不修改原始calls或偽稱修正後結果。若無安全call-boundary暫停機制，可完成後保留為變體；其bank暫不得作正式bank。
5. 審查受影響檢索：若校正流程的candidate、retrieved skills及所有token-affecting輸入與已完成maintenance request完全一致，可在新manifest以request/response hash引用復用，標reused live call。若不同則從該candidate起按校正後當時bank重新構造；僅對不相同輸入新增call，不能把舊decision搬到不同skills集合。沒有現場重跑的引用不得稱新live call。
6. 以獨立B/C bank輸出及provenance反例、同源maintenance可檢索、solver同源仍不可取用驗證修正。變體結果與校正結果分開，不能用本次錯誤放寬A09/A13/A18。

累計100上限不變。若最小重驗仍需超額，由Terra報實數及必要步驟，主 agent再決定。這次修正沒有改source、dev/held-out split、技能生成prompt或正式成功指標。

## R011 — 配對語義校準與格式修復（2026-09-07，核定）

R009完成23 calls：10個anchor全no_related_group、3個合格event候選及3次add、1個event skip、6個抽取錯誤（1 length、5 local-trigger/response-order驗證失敗）。尚未有task抽取call，不能宣稱task流程已live驗收。

主 agent回讀全部10個拒絕理由與Fig6。配對理由常以不同領域、最後處理方向不同，或沒有完全相同的專用流程為由拒絕。Fig6要求重複、可重用的多步pattern，沒有要求整題解法或領域完全相同。原始fix-git的2c782b31/e9105fe9與git-leak-recovery的e5277f5c/5c1c3143皆可見reflog/檢視commit操作；這支持檢查配對是否過窄，並不直接證明能生成合格task skill。短描述也會省略中間步驟；不得把no_group擴大解讀為原始trace必然沒有共通pattern。

核定一次、僅來源集上的校準：

1. 保留原配對prompt/results。新custom prompt澄清：兩到三條軌跡總數包含anchor；可共享其中一段有實質內容的多步子流程，不要求完整解法相同或同領域。只有同工具/語言或泛泛「讀、改、測」不足以選組。具體證據不足仍no_group；不能指定要選哪一組。
2. 使用原10份描述、相同MiniLM順位、相同全部10anchor順序，一致重評一次。新欄位可要求指出各描述支持哪個共通步驟；不得手寫預期skill、手挑Git pair或只重評看似有利的anchor。真正可抽取性仍由Fig6讀完整2–3軌跡判斷，可skip。
3. 若這次仍全無group，不再以反覆prompt調整追求select；記為來源池/配對覆盖缺口並回報，另決定新增來源或接受未觀察狀態，不能偽造A01通過。
4. 對原5個event sidecar格式/引用錯誤，各允許一次明示修復。傳入原完整trace、原模型輸出與精確validator錯誤；只可修正evidence欄位，skill內容不得修改。若原skill不是可支持的局部事件，保留invalid，不重新寫技能湊過。仍執行原role/order/source-ID檢查；原失敗與修復輸出分別保存。
5. build-pmars length輸出可用同模型及8192上限作一次明示重試，保留原截斷；不得提高output上限或把length當skip。此重試與格式修復不代表第二輪額外event探索。
6. 成功修復仍是原source首輪候選的修復版本；共同候選manifest依固定task-group後event-source順序組成。B/C按R010分別處理，原有效抽取與維護只有在實際輸入相同時可hash引用。未修好的錯誤仍保留於分母及驗收，不手動挑去。

所有calls仍計入100總額，concurrency1/output8192/timeout300不變；沒有held-out結果參與校準。這是我們的配對/格式工程決策，不宣稱作者提供過這套流程。
