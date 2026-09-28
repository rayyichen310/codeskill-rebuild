# 論文對照

更新：2026-09-28（Claude 接手後重寫）。取代 `docs/archive/REPRODUCTION_SPEC.md` 第 3 節的對照表；
舊版 P01–P17 條目與 A01–A19 驗收清單保留在封存檔。

論文：Li et al., *CODESKILL: Learning Self-Evolving Skills for Coding Agents*, arXiv:2605.25430v1，
24 頁 PDF，SHA-256 `73c48c5c919e1e60e078f1c07fbcd407ac3d0deba0aa007eff62b83a8b70ac4d`。
頁碼均指該 PDF。

## 1. 論文在做什麼

- **目標**：固定（frozen）的 coding policy π 不更新；訓練一個 manager Mθ 管理 skill bank，
  讓 π 在未來任務上的表現變好（§3.1，p.3）。
- **Skill 格式**：title、granularity、when_to_apply、rules 的 Markdown 指令檔；兩種粒度
  （§3.2，附錄 D，p.3、13）：
  - task-level（general）：一類任務的高層流程，通常由解相關題目的多條軌跡蒸餾而來。
  - event-driven：局部執行事件（指令失敗、錯誤訊息、測試輸出模式）發生時該怎麼反應。
- **操作**（Fig.6–9，p.16–19）：
  - Task 抽取：讀 2–3 條相關軌跡，generate 一個或 skip。
  - Event 抽取：讀一條完整軌跡，挑最可重用的一個事件，generate 或 skip。
  - Evolve：讀相關舊 skill + 新軌跡，修訂其中一個或 skip。
  - Maintain：每個新／演化候選配上檢索到的相似 skill，add／merge／drop。
- **使用**（附錄 C，p.13）：MiniLM（all-MiniLM-L6-v2）編碼 title + when_to_apply + rules；
  依 benchmark 與粒度分開建索引；排除同一 evaluation instance 產生的 skill。
  - Task query：task goal、problem statement、repo／benchmark context；解題前檢索一次，
    附加到初始 user prompt。
  - Event query：當前 task context、最近的 reasoning、actions、observations、錯誤訊息、
    指令輸出、測試輸出。
- **線上流程**（附錄 C，p.13）：對每個 instance 先收集 no-skill baseline rollouts，
  多次 prompt manager 產生候選（平均約 1 個 task、3 個 event），再讓 π 帶著檢索到的 skill
  解題；每條 skill-conditioned 軌跡都送去 evolve；新／演化候選再進 maintenance。
- **訓練**（§3.3、附錄 A–B）：Qwen3.5-4B，先用 GPT-5.4-mini 產的 12,856 筆資料 SFT，
  再 GRPO。Reward = λ·R_Q + R_A·R_E：
  - R_Q：rubric judge 的品質分數（Fig.10–13）。
  - R_E：用反向檢索找一題，跑 skill-conditioned rollout，與 4 次 no-skill 平均比 verifier 分數。
  - R_A：alignment judge（Fig.14），判斷 solver 是否真的照 skill 做。
  - 訓練最大序列 14k tokens（Table 4）。

## 2. 本重建的定位

本 repo **不訓練** manager，用 DeepSeek-V4-Flash + prompt 直接管理 skill，solver 也是
DeepSeek-V4-Flash。最接近論文 Table 1 的 **Prompt Skill Mgmt.** 這一列（固定 prompt，
沒有下游回饋），而不是訓練後的 CODESKILL。

論文中與本 repo 最相關的數字（Table 1，p.6；TB2 共 85 題，1 題 ≈ 1.18 點）：

| Solver | No-skill | Prompt 管理（GPT-5.4-mini） | Prompt 管理（Qwen3.5-4B） | CODESKILL（訓練後） |
|---|---|---|---|---|
| Qwen3.5-35B-A3B，TB2 | 25.88 | 28.24（+2 題） | 24.71 | 34.12（+7 題） |
| GPT-5.4-mini，TB2 | 20.00 | 21.18（+1 題） | 23.53 | 25.88 |
| Qwen3.5-35B-A3B，SWE-bench Verified | 57.33 | 64.67（+7.3） | 58.67 | 66.00（+8.7） |
| GPT-5.4-mini，SWE-bench Verified | 46.67 | 56.67（+10.0） | 50.67 | 56.00（+9.3） |
| Qwen3.5-35B-A3B，四項平均 | 29.57 | 35.25 | 29.72 | 39.26 |
| GPT-5.4-mini，四項平均 | 21.80 | 27.86 | 25.90 | 30.73 |

解讀：
- GPT-5.4-mini 本身是強模型（論文也拿它當 teacher 與 judge）。
- 要不要 RL 取決於 benchmark：
  - SWE-bench Verified 上，強模型 + prompt 的增益與 RL 版相當（+7.3 vs +8.7；+10.0 vs +9.3）。
  - TB2（論文的 out-of-distribution 測試集）上，prompt 管理只多 1–2 題，RL 版多 5–7 題。
  - 四項平均，prompt 管理約拿到 RL 增益的六到七成。
- 前提是 pipeline 正常：論文的 prompt baseline 與 CODESKILL 用同一套 prompt 和檢索流程。
- 本 repo 在 TB2 上對通過率的合理預期是小幅改善，要看出來需要比 12 題更大的樣本；
  SWE-bench Verified 較有機會。
- Skill **品質**（rubric 分數、add/merge/drop 判斷）則應受惠於強模型，可以期待到一定水準，
  而且可以離線量測（見 `docs/DECISIONS.md` 待決事項 P4）。

## 3. 逐項對照

類型：**P** 忠於論文；**D** 論文沒寫、我們補的決定；**V** 刻意變體；**缺** 尚未實作；
**問題** 2026-09-28 審查發現的缺陷。

| 項目 | 論文 | 本 repo 目前（WIP 分支 `claude/wip-snapshot-20260928`） | 類型 | 評語 |
|---|---|---|---|---|
| Manager | 訓練後 Qwen3.5-4B | DeepSeek-V4-Flash + prompt，temperature 0，reasoning max | V | 對應 Prompt Skill Mgmt. baseline |
| Solver／harness | Qwen3.5-35B-A3B 或 GPT-5.4-mini；mini-SWE-agent | DeepSeek-V4-Flash；OpenClaw 2026.9.3 + Harbor；用 proxy 注入 | V | manager 與 solver 同模型，skill 等於 solver 自我蒸餾 |
| 軌跡表示 | 正規化成 reasoning／action／observation；訓練輸入 ≤14k | OpenClaw raw session 的 lossless JSON 投影，預設保留 thinking | V | 實際請求約為原始內容的 2.7 倍（見 §5） |
| 找相關軌跡 | 未說明 | 每條軌跡先產生 SOP 候選 → MiniLM 排序 → LLM 配對（D02） | D | 合理的補洞；TB2 12 題幾乎找不到相關組 |
| Task 抽取 | Fig.6 讀 2–3 條相關軌跡 | 從 2–3 份 SOP 合併（`prompts/custom/r015_task_merge_from_sops.md`）；WIP 版合併時看不到原始軌跡 | D／V | 單條 SOP 會先把各題的修法固化，跨題共同動作在合併時已看不到 |
| Event 抽取 | Fig.7 讀完整軌跡，每次一個，多次 prompt（每題約 3 個） | 每個 segment 一次呼叫（`r015_event_from_segment.md`）；Event graph 預設每條軌跡最多 8 個 | V | 與 R012「每條最多 3 次」衝突，見 DECISIONS |
| 禁止具體識別字（Fig.6 第 5 條、Fig.7 第 6 條） | 禁止 repo 名、變數／函式／類別名、確切路徑、一次性字面值 | custom prompt 放寬為「Omit incidental names」，code-example 版更明寫「Variables, functions, paths… are allowed」 | V／**問題** | skill 變成單題 patch 食譜的主因 |
| 證據 sidecar | 無 | 每條 rule 引用來源 step；多來源 task 要求每個來源都引用 | 擴充 | 過嚴時整批拒收（見 EXPERIMENTS 9/20、9/26） |
| Code examples | Limitations 明說只做自然語言 skill | D04a：證據綁定的可改寫程式範例 | 擴充 | 無證據顯示有幫助 |
| Evolve | Fig.8：相關 skill + skill-conditioned 軌跡 | 只考慮本 trial 實際注入過的 skill（R012） | D | 檢索錯了，演化就全部 skip |
| Maintain | Fig.9 add／merge／drop | 同，MiniLM top-5 相似 skill；另加 code example 保留／修訂 | P | SWE pilot：9 add、1 merge、0 drop |
| 索引 | MiniLM；title + when + rules；依 benchmark 與粒度分開 | 同；256 wordpiece，三欄配額 15%／35%／50% | P＋D | — |
| Task query | goal、problem、repo context | 第一則 user 訊息 + system 訊息（`openclaw_sidecar_retrieval.py:346`） | **問題** | SWE 題目本文被截掉，只剩 harness 樣板；TB2 約 45% 是 OpenClaw system prompt |
| Event query | context + 近期 reasoning／action／observation／錯誤／測試輸出 | observation 取開頭 token（`retrieval.py:73`）；reasoning 欄重複 action 文字、實際為空（`openclaw_sidecar_retrieval.py:372`）；每次工具結果都觸發 | **問題** | 錯誤訊息通常在輸出結尾；讀檔也會觸發 |
| 門檻 | 未說明 | task 0.45 取前 2；event 0.50 取前 1（開發值，未校準） | D | — |
| 檢索後相關性判斷 | 無（只用 MiniLM） | 離線評測中：MiniLM 前 3 → TypeSafe Jev 選一條或都不選；尚未進正式路徑 | D（候選） | 另一個模型把關，屬於方法偏離；見 EXPERIMENTS §3 |
| 同題排除 | 排除同 instance | 依完整祖先來源排除（含 merge／evolve） | P＋D | — |
| 線上流程 | 每題先跑 no-skill baseline，從中抽取，再帶 skill 解題 | TB2 C-only：只從 C 軌跡抽取，每輪空 bank，與歷史 baseline 比；SWE pilot：baseline 與 codeskill 兩組分開跑 | V | 缺同 runtime 的成對對照 |
| Rubric judge（Fig.10–13） | 當 reward R_Q | 未實作 | **缺** | 目前沒有 skill 品質量測 |
| Alignment judge（Fig.14） | 當 reward R_A | 未實作 | **缺** | 無法區分「沒用到」與「用了沒幫助」 |
| 指標 | 通過率、solved-only steps、bank size | 只有通過率 | 缺 | — |

## 4. Prompt 檔的狀態

- `prompts/paper/` 的 README 宣稱是 Fig.6–9 的忠實轉寫，但 `fig06_task_extraction.md`、
  `fig07_event_extraction.md` 已被就地改寫（commit 494951e、17c7bd6、ef56b07），加入證據
  schema 與「從 SOP 合併」的語意。論文原文仍可從 `prompts/paper/source-extract-pages16-24.txt`
  與 commit 0aa2950 的版本取回。
- 實際使用中的 prompt 在 `prompts/custom/`（`r015_*`）。它們改寫了選題標準與識別字規則，
  屬於方法變更，但沒有留下使用者核准的紀錄。
- 建議（待決 P1）：恢復 `prompts/paper/` 的忠實版本，當作論文對照組；custom prompt 另外標版本。

## 5. 論文沒說清楚、我們必須自己補的地方

1. **2–3 條軌跡如何放進 manager 的 context。** 論文訓練序列只有 14k，必然做過壓縮或截斷，
   但沒寫方法。粗估（字元數 ÷ 3.5，未用 tokenizer）：
   - 去掉 thinking，並把每個工具輸出截成頭尾各 1,500 字元後，SWE pilot 10 條為 6–37k，
     TB2 歷史 baseline 10 條為 2–34k。
   - 但實際送給 manager 的 JSON 請求約是原始內容的 2.7 倍（例：sphinx-8265 為 29.6 萬字元）。
   - 需要用 tokenizer 精確確認（待決 P3）。
2. **怎麼找「相關」軌跡。** 論文只說 task skill「通常來自解相關題目的軌跡」。題組本身有沒有
   相關題，比配對演算法更重要。
3. **門檻、每次注入幾條、token 預算。** 論文沒有給。
4. **Event 檢索的觸發時機。** 論文只列 query 的組成，沒說每一步都查，還是只在出現事件時查。
