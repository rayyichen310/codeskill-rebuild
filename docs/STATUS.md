# 現況

更新：2026-09-28。Claude 於本日接手 Codex 的實作。本檔是現況快照；舊的逐日紀錄在
`docs/archive/STATUS-2026-09-11.md` 與 `docs/archive/IMPLEMENTATION_STATUS.md`。

## 總結

- 工程管線已能在 TB2 與 SWE-bench 上端到端跑完：抽取 → bank → OpenClaw 注入 → 演化／維護。
- 目前**沒有證據顯示 skill 對 solver 有幫助**。兩次成效實測都無法判讀
  （`docs/EXPERIMENTS.md` §1）。
- 已找到兩個會讓 skill 失效的具體缺陷：
  - 抽取 prompt 放寬了論文的識別字禁令，產出變成單題 patch 食譜。
  - 檢索 query 組法有 bug，注入的 skill 與當下情境無關。
- 離線檢索評測（2026-09-28，`docs/EXPERIMENTS.md` §3）顯示：
  - query 修正能大幅改善排序；
  - 但現有 bank 幾乎沒有跨題可用的 skill，只靠 MiniLM 門檻無法得到可用的精確度。

## 程式與環境

- 工作分支：`claude/wip-snapshot-20260928`（worktree `~/ray/codeskill-work`），基底是 Codex 筆電
  checkout 的未驗證 WIP（commit 22e5078）。權威 repo 仍是 `~/ray/codeskill-rebuild-20260905`。
  其主 checkout 停在 `codex/rebuild-v1`，有舊的未提交文件修改，內容已包含在本分支。
- 離線測試（2026-09-28）：324 項，1 fail + 2 error。系統 python3 缺 langgraph、httpx、pytest，
  需用 `~/ray/tmp/r015-event-graph-r1-four-20260925-01/.venv`。詳見 `docs/ARCHITECTURE.md` §8。
- 最新實驗（9/28 SWE pilot）的 orchestration 腳本不在 git。

## 2026-09-28 審查發現

依影響排序。證據來自 `~/ray/tmp/codeskill-swe-pilot-20260928` 與
`~/ray/tmp/r015-c-only-formal-20260914-01`。

### 1. Skill 是單題 patch 食譜

- SWE pilot 的 11 條 skill 幾乎都是該題的修法，含函式名、類別名與檔案路徑。例如：
  「在 `DocstringSignatureMixin._find_signature`（`sphinx/ext/autodoc/__init__.py`）初始化
  `self._additional_signatures = []`」。
- 原因：`prompts/custom/r015_event_from_segment.md:20` 把論文 Fig.7 第 6 條
  （禁止變數／函式／類別名、確切路徑、一次性字面值）改成「Omit incidental names」。
  `r015_fig07_*_with_code_examples.md:12` 更明寫允許變數、函式、路徑。
- 唯一的 task skill 來自 sphinx-9367 + 8265，兩題是同一個 bug 的延續，所以同樣是 patch。
- TB2 的 event skill（例如用 git reflog 找回 commit）較通用：SWE 這種「定位 bug、修 bug」的
  軌跡特別容易被抽成修法。

### 2. 檢索 query 的缺陷

- **Task query 沒有題目內容。** Query 由第一則 user 訊息加 system 訊息組成
  （`src/codeskill_rebuild/openclaw_sidecar_retrieval.py:346`）。
  - SWE：題目前面有 harness 說明，前 170 token 被樣板占滿，題目本文被截掉。
  - TB2：約 45% 是 OpenClaw system prompt。
- **Event query 取觀察輸出的開頭**（`src/codeskill_rebuild/retrieval.py:73`）。錯誤與 traceback
  通常在結尾；讀測試檔時，query 內容是版權宣告。
- **Reasoning 欄是空的。** 它只重複 action 文字，沒有讀 `reasoning_content`
  （`openclaw_sidecar_retrieval.py:372`）。
- **每次工具結果都觸發 event 檢索**，包括單純讀檔。
- 實例（sphinx-9367）：真正相關的 tuple unparsing skill 得分 0.24，被門檻擋掉；不相關的
  autodoc skill 得 0.525，被注入。
- 離線重放已量化：
  - SWE task query 對所有題目分數都約 0.07，完全沒有鑑別力。
  - event 相關時機的 hit@1 只有 0.09；修正後為 1.00。

### 3. 檢索精確度：13 次注入全部無關

SWE pilot 共注入 event skill 13 次（舊版文件誤記為 14）。Fig.8 演化時，manager 逐一判斷，
13/13 都說「與本題無關」並 skip，因此整個 pilot 沒有任何 skill 被修訂。Task skill 注入 0 次。
離線評測的人工標註也判 13/13 無關。

### 4. 沒有品質量測

論文的 rubric judge（Fig.10–13）與 alignment judge（Fig.14）都沒有實作。「品質差」目前只有
人工觀察，prompt 修改也無法客觀比較。

### 5. 驗證器過嚴，產量被壓到零

- 9/20 thinking A/B：兩組驗證通過的候選都是 0。
- 9/26 生成器 ablation：3 個生成結果全部因「每條 rule 需一個來源紀錄」被拒。
- R009：6 個錯誤中有 5 個是證據 sidecar 驗證失敗。

### 6. 實驗設計測不出論文等級的效果

- 論文中 prompt 管理（GPT-5.4-mini）在 TB2 只比 no-skill 多 1–2 題（共 85 題）。
- 本專案同條件重跑約有 20–30% 的題目結果翻轉（TB2 C 第 1、2 輪 3/11；SWE 空 bank 的第 1 題
  也翻轉）。24 組 A／C 配對大約只能看出 20 點以上的差距。
- TB2 這 12 題的歷史 baseline 已解 8/11，可改善的空間最多 3 題。

### 7. Prompt 對照組不見了

`prompts/paper/fig06`、`fig07` 被就地改寫（commit 494951e、17c7bd6、ef56b07），但 README 仍稱
為忠實轉寫。現在沒有可執行的論文原版 prompt。

### 8. 過度工程化

- src + scripts + tests 約 5.1 萬行，對應論文方法的核心約 1.5k 行。
- 單一 driver 5,182 行、294 處 raise。9/10 以來 82 個 commit，成效實測只有 2 次。
- 大量 provenance 與復原邏輯變成拒收關卡（見第 5 點），而診斷紀錄存了 token ID，卻沒人讀過
  query 的實際文字（見第 2 點）。
- WIP 分支顯示 Codex 已開始精簡（Event graph 由 421 行減到 146 行）。

## 對結果的合理預期

- Skill **品質**：DeepSeek-V4-Flash 應達一定水準；目前差是 prompt 造成的，可以離線驗證。
- **通過率**：本 repo 對應論文的 Prompt Skill Mgmt.，沒有下游回饋訓練，manager 與 solver 又是
  同一模型。論文中 prompt 管理在 SWE-bench Verified 與 RL 版相當，在 TB2 只多 1–2 題
  （`docs/PAPER_ALIGNMENT.md` §2）。TB2 要看出效果，需要更大樣本或改善空間更大的題組。
- 建議依序驗收：skill 品質（rubric）→ 檢索精確度 → solver 是否照做（Fig.14）→ 通過率。

## 離線檢索評測（2026-09-28 完成）

完整設定與數字見 `docs/EXPERIMENTS.md` §3。重放以現行程式組 query，與 live 紀錄逐字相同
（event 1,249/1,249、task 34/34）。

- **Query 修正有效。** 修正內容：
  - task query 只用題目本文；
  - event query 取「錯誤行 + 輸出結尾」、純文字指令、`reasoning_content`；
  - task context 改成題目本文。

  相關時機的 hit@1：
  - production SWE：0.09 → 1.00；
  - production TB2：0.62 → 0.92；
  - crossrun SWE：0.62 → 0.86。
- **精確度受 bank 限制。** 排除同題 skill 後，每條軌跡約只有 1% 的時機有可用 skill。
  - SWE 在任何門檻下 precision 都 ≤ 0.03。
  - TB2 修正版在門檻 0.50 時，precision 0.40、recall 0.46；現行版在注入量相近的門檻下，
    precision 0.09、recall 0.23。
- **「只在錯誤時觸發」不採用。** 它會漏掉 64–85% 的相關時機；相關時機多半是讀程式或看指令輸出。
- **門檻需要重設。** Skill 全文或只用 when_to_apply 都可以，差異不穩定，維持全文。
- **Jev 相關性判斷（EXPERIMENTS §3）：** MiniLM 取前 3，再由 Jev 選一條或都不選。
  - production SWE：precision 0.03 → 0.40，注入量降為 1/14；
  - crossrun SWE：recall 0.63 → 0.86；
  - 延遲 p50 0.32 秒；
  - 抽查顯示多數「誤判」其實合理。
- 結論：修正檢索只是必要條件。精確度要靠：
  - 第二階段相關性判斷（P4）；
  - 能跨題重用的 skill（P1）；
  - 有相關題的題組（P5）。

## 下一步

修正順序已與使用者議定（`docs/DECISIONS.md` §3）：檢索 → 抽取品質 → 免訓練回饋 → RL。
方法相關項目仍需逐項確認（§4 的 P 編號）。

| 階段 | 內容 | 是否跑 solver |
|---|---|---|
| 0 | 文件整理（完成，commit 78e7e90）；SWE pilot 腳本進 git；修好測試環境與失敗測試 | 否 |
| 1 | 離線檢索評測（完成，EXPERIMENTS §3）；用 tokenizer 量精簡軌跡長度（P3） | 否 |
| 2 | 檢索修正（P2，已實作 62c1d4e）；P1 方案 B 已實作（`scripts/run_p1_extraction.py`），2026-09-28 正式抽取進行中，輸出 `~/ray/tmp/p1-extraction-20260928-01/`（DECISIONS §3）；抽取修正：恢復識別字禁令 + lint、精簡軌跡、rubric verifier 與有限次修正（P1、P3、P4）；先離線比較 | 否（rubric 需呼叫 DeepSeek，judge 模型待定） |
| 3 | 免訓練回饋：skill 效用統計、對照式抽取、Best-of-N（P12） | 部分需要 |
| 4 | 重新實測：同 runtime 成對 A／C，選有相關題的題組（P5、P11）；可先做 solver 上限實驗（P8） | 是，需確認 |
| 之後 | RL 只在前述條件成立時考慮（P13） | — |
| 並行 | 凍結重型 driver；新工作寫成小腳本；階段 4 前完成精簡 runner（P7，時程已同意） | 否 |
