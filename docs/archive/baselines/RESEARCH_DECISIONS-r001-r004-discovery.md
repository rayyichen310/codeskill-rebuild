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
