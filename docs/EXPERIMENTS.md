# 實驗索引

更新：2026-09-28。所有實際跑過的 run，依日期排列。原始資料只存在 T2，不進 git。
2026-09-11 以後的 run 在 `~/ray/tmp/`，不在 repo 的 `runs/`（`runs/` 位於
`~/ray/codeskill-rebuild-20260905/runs/`，只到 9/11）。

證據分級：**live** 指真的呼叫模型或跑了 solver／verifier；**offline** 指 fixture、fake transport
或不呼叫模型的 probe。

## 1. 成效實測（有 solver 與官方 verifier）

### TB2 C-only 兩輪（2026-09-14～09-15）

- 位置：`~/ray/tmp/r015-c-only-formal-20260914-01`
- 設定：`configs/r015-c-only-coding.json`。12 題 TB2（9/4 歷史 baseline 那 12 題），每輪從空 bank
  開始，只跑 C 組（抽取 + 演化 + 維護），每題 1 次。Harbor 0.17.1、官方 OpenClaw 2026.9.3；
  不設請求次數上限，agent timeout multiplier 4.0（與 baseline 相同）。
- 結果（`verifier/reward.txt`）：

| 題目 | 歷史 baseline（9/4） | C 第 1 輪 | C 第 2 輪 |
|---|---|---|---|
| build-pmars | 1 | 0 | 0 |
| cancel-async-tasks | 1 | 1 | 0 |
| cobol-modernization | 1 | 1 | 1 |
| code-from-image | 0 | 0 | 0 |
| fix-git | 1 | 1 | 1 |
| fix-ocaml-gc | 1 | 0 | 1 |
| git-leak-recovery | 1 | 0 | 0 |
| headless-terminal | 1 | 0 | 0 |
| kv-store-grpc | 0 | 0 | 0 |
| polyglot-c-py | infra（setup timeout） | 0 | 1 |
| pypi-server | 1 | 0 | 0 |
| schemelike-metacircular-eval | 0 | 1 | 未跑 |
| **合計** | 8/11 | 4/12 | 4/11 |

- Bank：第 1 輪結束 13 條（12 event、1 task），第 2 輪 11 條（10 event、1 task）。
- **不可判讀**，原因：
  - baseline 是不同 runtime：OpenClaw 2026.7.2 原始碼版，C 為官方 npm 2026.9.3；timeout 等設定
    歷史 manifest 未完整保存。
  - Harbor adapter 當時強制 UID 1000，導致 build-pmars、git-leak-recovery、pypi-server 的環境
    失敗；9/16 才修（`docs/evidence/r015-environment-alignment-20260916.json`）。
  - 沒有同 runtime 的 no-skill 對照。
  - 同條件兩輪之間有 3/11 題結果翻轉。

### SWE-bench Verified pilot（2026-09-28）

- 位置：`~/ray/tmp/codeskill-swe-pilot-20260928`。Orchestration 腳本 `swe_*.py`（約 2k 行）
  只在該目錄，不在 git。
- 設定：10 題，依主題挑相關題：django 表單／widget 5 題、sphinx autodoc／pycode 5 題。
  baseline 組（無 skill）與 codeskill 組（空 bank 起步、逐題累積）各跑 1 次。
  檢索：task 門檻 0.45 取前 2；event 門檻 0.50 取前 1。
- 結果：baseline 6/10，codeskill 8/10。多解的是 django-11790 與 sphinx-8265；其中 11790 是第 1 題，
  當時 bank 為空，屬純雜訊。
- Bank 11 條（10 event、1 task），幾乎都是單題修法，含函式名與檔案路徑。唯一的 task skill 來自
  sphinx-9367 + 8265 配對，這兩題是同一個 bug 的延續。
- 注入：task skill 0 次；event skill 13 次（overlay 紀錄與 evolution 輸入都是 13；舊版文件誤記為 14）。
  Fig.8 演化時，manager 對 13 次注入全部判定「與本題無關」並 skip。
- Maintenance：9 add、1 merge、0 drop。
- **不可判讀**（n=10、1 次、注入內容無關）。但對檢索精確度與 skill 品質是直接證據，
  見 `docs/STATUS.md`。

### R015 A／B／C 開發接線試跑（2026-09-12～09-13）

- 位置：`~/ray/tmp/r015-dev-luna-max-20260912-01` 到 `-20260913-24`，共 24 次嘗試。
- 題目：password-recovery、portfolio-optimization，A／B／C 各 1 次。
- 設定（`docs/archive/M3_DEVELOPMENT_PROFILE.md`、`configs/m3-r015-development.json`）：每 trial
  最多 24 次 solver 請求、output 16384、agent／verifier 各 900 秒、outer 2700 秒；task 門檻 0.45
  取前 2，event 門檻 0.50 取前 1，event 注入上限 25000 tokens。
- 目的是打通 OpenClaw／Harbor／sidecar 接線；多次嘗試之間結果不一致。**不作效果判讀**。

## 2. Manager 相關實驗（live 呼叫 manager，不跑 solver）

| 日期 | 位置 | 問題 | 結果 |
|---|---|---|---|
| 09-05 | `runs/m1-*` | 服務與 tokenizer 是否可用 | 短 probe HTTP 200；exact preflight 與 usage 相符；MiniLM 固定 revision `1110a243…` 可載入 |
| 09-05 | `runs/m2-offline-pilot-20260905-01` | 4 條來源的描述、配對、event、maintenance | 配對 no_related_group；1 event add；粒度偏寬 |
| 09-05～06 | `runs/m2-context-stress-*` | 超長 trace 的切段摘要路徑 | 第 4 次成功產生 event；只證明機制能跑 |
| 09-07 | `runs/m2-full-descriptions-20260907-01` | 10 條來源的描述 | 10/10 完成；最大 input 161,609 tokens |
| 09-07 | `runs/m2-common-bank-r009-20260907-01` | 全池配對與 event | 23 calls：10 個 anchor 全部 no_related_group；3 event add；6 個錯誤（其中 5 個是證據 sidecar 驗證失敗） |
| 09-08 | `runs/m2-r011-calibration-20260908-02` | 配對 prompt 校準後重評 | 仍是 0 group；6 個 event 候選，C 組 6 次 maintenance 全部 add；最大 input 290,326 |
| 09-11 | `runs/m2-r014-event-extraction-20260911-01` | R014 長 trace fallback | 見該 run 的 console log |
| 09-20 | `~/ray/tmp/r015-thinking-ab-20260920-02` | manager 輸入去掉 solver thinking 的影響（8k output） | 各 10 calls；prompt tokens 1,035,579 → 679,009（−34.4%）；完整輸出 6/10 → 7/10；**兩組驗證通過的候選都是 0** |
| 09-23 | `~/ray/tmp/codeskill-thinking-ab-16k-20260923-01` | 同上，output 16k | keep 10/10 完整；exclude 9/10 完整、1 次截斷；候選結果未整理 |
| 09-25 | `~/ray/tmp/r015-event-graph-r1-four-20260925-01～07` | Event graph 用 4 題 TB2 重跑 | 7 次嘗試，屬開發除錯 |
| 09-26 | `~/ray/tmp/r015-event-generator-ablation-20260926-01` | 生成器本身的輸出 | 3 次呼叫都生成了 skill，但受控路徑因「每條 rule 需一個來源紀錄」**全部拒收** |
| 09-26 | `~/ray/tmp/r015-event-prompt-followup-20260926-03` | event prompt 修改後重測 | fix-git 2 個、git-leak-recovery 1 個候選通過；build-pmars、schemelike 0 個 |
| 09-27 | `~/ray/tmp/r015-event-prompt-simplification-20260927-01` | 簡化 prompt 與原 prompt 成對比較 | 3 個 case × 2 組，各 1 次；生成器診斷 |
| 09-27 | `~/ray/tmp/r015-event-same-model-review-20260927-01` | 用同一模型審查上述 6 個候選 | 3 個 keep；2 個指出實質錯誤（例如 cherry-pick 規則會刪掉衝突區以外的內容） |
| 09-27 | `~/ray/tmp/r015-event-selection-only-20260927-01` | 只做 event 選擇的對照 | 未整理 |

## 3. 離線檢索評測（2026-09-28，offline，只用本機 MiniLM）

- 位置：`~/ray/tmp/retrieval-eval-20260928/`（`result.json`、`event-rows.jsonl`）。
  腳本 `scripts/eval_retrieval_offline.py`，commit 2bdd910（乾淨工作樹），MiniLM revision
  `1110a243…`，CPU 執行，約 2.5 分鐘。
- 資料：
  - 54 條軌跡，1,990 個工具批次（tool batch）。SWE pilot 20 條（baseline、codeskill 各 10），
    TB2 C-only 兩輪 24 條，9/4 歷史 baseline 10 條。
  - 有 sidecar 的 run 從 upstream request 重建，歷史軌跡從 steps 重建。
  - Bank：SWE 11 條（round 1 最終 bank）；TB2 共 24 條（第 1 輪 13 條 + 第 2 輪 11 條）。
- **重放正確性**：以現行程式組出的 query，與 sidecar 當時記錄的 query 逐字比對，
  event 1,249/1,249、task 34/34 完全相同。
- **Live 注入的標註**：SWE pilot 的 13 次注入，本次標註全部判為無關，與 Fig.8 的判斷一致。
- 兩種設定：
  - **production**：排除同題產生的 skill（線上規則）。只有真正跨題可用的 skill 會算相關，
    實際只有兩組：sphinx-9367 ↔ tuple unparsing，以及 fix-git ↔ git-leak-recovery 的 reflog skill。
  - **crossrun**：只排除「從這條軌跡本身」抽出的 skill，同題其他 run 的 skill 算相關。
    這在正式評估中屬於洩漏，只用來診斷 query 組法。
- 比較項目：
  - Query：現行（current）、修正版（fixed）、修正版但不含 task context（fixed_no_task）。
    修正版的內容：
    - task query 只用題目本文，SWE 另加 repo 名；
    - event query 取「錯誤行 + 輸出結尾」、純文字指令、`reasoning_content`，task context 改成題目本文。
  - Skill 端：全文（title + when + rules），或只用 title + when_to_apply。
  - 觸發：每個工具結果都查（all），或只在錯誤類輸出時查（error）。
- 標註：每條 skill 一個觸發條件（regex，寫在腳本 `LABELS`）。
  - 錯誤型 skill 可跨題，例如 `xxd: not found`、`Committer identity unknown`。
  - 內容型 skill 只在可能適用的題目內算相關。
  - 標註由 Claude 撰寫，未經第二人核對。

### Event 檢索結果（skill 全文，每個工具結果都查）

排序品質（不依賴門檻）：

| 設定 | 題組 | 批次 | 相關批次 | 相關批次 hit@1（現行 → 修正） | AUC（現行 → 修正） |
|---|---|---|---|---|---|
| production | SWE | 878 | 11 | 0.09 → **1.00** | 0.79 → 0.97 |
| production | TB2 | 1,112 | 13 | 0.62 → **0.92** | 0.99 → 1.00 |
| crossrun | SWE | 878 | 177 | 0.62 → 0.86 | 0.88 → 0.95 |
| crossrun | TB2 | 1,112 | 98 | 0.88 → 0.88 | 0.98 → 0.99 |

修正版分數整體偏高，所以比較注入時用「注入量相近」的門檻：

| 設定 | 題組 | 現行（門檻：每條注入數／precision／recall） | 修正版（同上） |
|---|---|---|---|
| production | SWE | 0.50：9.5／0.00／0.00 | 0.60：3.4／0.03／0.18 |
| production | TB2 | 0.45：0.94／0.09／0.23 | 0.50：0.44／**0.40**／**0.46** |
| crossrun | SWE | 0.50：14.3／0.24／0.38 | 0.60：15.6／0.36／0.63 |
| crossrun | TB2 | 0.50：3.8／0.34／0.45 | 0.55：3.6／0.39／0.48 |

- **Production SWE 在任何門檻下 precision 都 ≤ 0.03。** 878 個批次中只有 11 個有可用的 skill
  （1.3%）。其他時間任何注入都是錯的，MiniLM 門檻無法單獨解決。
- **只在錯誤時觸發會漏掉大部分相關時機。** 每條軌跡的觸發次數從 44／33 降到 13／9，但通過的
  相關批次只剩：production SWE 2/11、TB2 3/13；crossrun SWE 50/177、TB2 33/98。
  相關時機多半是讀程式或看指令輸出，不是錯誤。
- **只比對 when_to_apply 沒有穩定好處**：hit@1 有時持平、有時較差（TB2 production 0.92 → 0.85）。
- **Task context 要保留，但改成題目本文**：拿掉後 hit@1 下降
  （production SWE 1.00 → 0.55；TB2 0.92 → 0.77）。

### Task 檢索結果

- SWE 現行 query 對所有題目的分數都約 0.07，完全沒有鑑別力：query 內容是 harness 說明與
  OpenClaw system prompt。
- 修正後，sphinx-9367 baseline 對 tuple task skill 得 0.487（達 0.45 門檻，會注入）；其他無關題目
  最高 0.21。
- TB2：相關的 task skill 在 7 個 crossrun 案例都排第 1（兩種 query 皆然），但分數只有 0.21–0.42，
  全部低於 0.45 門檻，一次都不會注入；無關 skill 最高 0.17。
- 每個 bank 只有 1–2 條 task skill，樣本很小。

### 結論與限制

1. Query 修正明顯改善排序，SWE 上最明顯（harness 樣板占滿 query）。TB2 的第一則 user 訊息本來就是
   題目，改善較小。
2. 精確度的上限來自 bank 內容，不是 query。現有 bank 幾乎沒有跨題可用的 skill，每條軌跡平均只有
   約 1% 的時機有東西可注入。要可用，需要：
   - 第二階段相關性判斷（P4 第 3 層）；
   - 抽出可跨題重用的 skill（P1）；
   - 題組內有相關題（P5）。
3. 「只在錯誤時觸發」的提案被推翻，不採用。
4. 門檻要在 query 修正後重新校準：原 event 0.50、task 0.45 都是未校準的開發值。
5. 限制：
   - 標註是 regex 觸發條件，只由一人（Claude）撰寫。
   - production 的正例只有 2 組。
   - crossrun 屬於診斷用。
   - LLM 相關性判斷另見下一小節。

### LLM 相關性判斷：Jev（2026-09-28，live，外部 API）

- 使用者指定用 TypeSafe 的 Jev 當判斷模型，版本固定為 `jev-1.13.0`。Jev 是專做結構化決策的模型：
  回傳選項的機率與 confidence，不生成文字。
- 腳本 `scripts/eval_relevance_judge.py`，commit efe022f（乾淨工作樹）。
  - 輸出：`judge-result.json`、`jev-responses.jsonl`，放在同一目錄。
  - API key 放在 `~/.config/codeskill/typesafe.env`，不進 git。
- 做法：
  - 候選：修正版 query 下，MiniLM 第一名分數 ≥ 0.35 的工具批次，各取前 3 條。
  - 每次請求問兩種問題：
    - 一題 Choice：三條候選 +「都不適用」選一個；
    - 每條候選一題 Noul：「目前情境是否符合這條的 when_to_apply」。
  - State 只放題目、純文字指令、reasoning 結尾、輸出的錯誤行 + 結尾。
- 規模：2,210 次請求，328 萬輸入 token，約 US$0.14。延遲 p50 0.32 秒、p90 0.36 秒、最大 1.4 秒。

結果：注入條件為「Jev 的 Choice 選中某條」。「MiniLM」欄沿用上一小節中注入量最接近的一列，
各數字為「每條軌跡的注入次數／precision／recall」。

| 設定 | 題組 | 只用 MiniLM | MiniLM ≥ 0.40 → Jev | MiniLM ≥ 0.40 → Jev，Noul ≥ 0.5 |
|---|---|---|---|---|
| production | SWE | 0.60：3.4／0.03／0.18 | 0.25／**0.40**／0.18 | 0.05／1.00／0.09 |
| production | TB2 | 0.50：0.44／0.40／0.46 | 0.35／**0.50**／0.46 | 0.12／0.75／0.23 |
| crossrun | SWE | 0.60：15.6／0.36／0.63 | 16.6／0.46／**0.86** | 9.9／**0.64**／0.72 |
| crossrun | TB2 | 0.55：3.6／0.39／0.48 | 5.1／0.41／**0.71** | 4.4／0.42／0.64 |

- **注入量相近時，精確度與 recall 都提升。**
  - production SWE：precision 0.03 → 0.40，而每條軌跡的注入量只有原本的 1/14。
  - crossrun SWE：recall 0.63 → 0.86。
  - TB2 小幅提升。
- **表上的 precision 低估了 Jev。** 我抽查了 6 筆被算成誤判的 TB2 例子，其中 5 筆其實合理，
  只是 regex 標註沒涵蓋。例如：
  - build-pmars 沒有編譯器也沒有 root，Jev 推薦「用 `apt-get download` 抽出套件」；
  - pypi-server 遇到 `ps: not found`，Jev 推薦「用 /proc 查連接埠」。

  唯一明確的錯誤：cherry-pick 衝突時推薦了 reset 救回 skill。
- **Jev 抓到跨題可用的 skill。** fix-ocaml-gc 裡出現 `xxd: not found` 時，選中 cobol 題抽出的
  「xxd 不存在時改用 od -c」，confidence 1.00。
- **Recall 受 when_to_apply 寫法限制。** sphinx-9367 的 tuple skill，when_to_apply 寫的是
  「tuple 預設參數缺括號」，而 9367 的問題是單元素 tuple 的尾逗號；rules 其實涵蓋，但 Jev 照字面讀，
  所以多數時機判「不適用」（production SWE recall 只有 2/11）。這說明抽取時的 when_to_apply
  品質（P1）會直接影響判斷效果。
- **Noul 分數偏低且保守。** 很多正確選擇的 Noul 只有 0.2 左右。預設只用 Choice；要更嚴格時再加
  Noul 門檻。
- 限制：
  - 標註問題同上；
  - production 正例很少；
  - 只評判斷本身，未評 solver 是否因此受益。

### P2 實作驗證（2026-09-28，commit 62c1d4e）

- 位置：`~/ray/tmp/retrieval-eval-20260928-p2/`。兩支評測腳本已改為呼叫正式模組
  `retrieval_query.py`、`relevance_judge.py`。
- 驗證結果：
  - 檢索重放的 `event-rows.jsonl` 與先前逐列相同；432 組摘要、task 結果也都相同。
  - Jev 的 2,210 次事件請求全部命中快取，代表正式程式送出的內容與評測時逐位元組一致；
    60 組摘要相同。
- **Task 階段加上 Jev**（新增 10 次呼叫；MiniLM 預篩 0.20，取前 3）：
  - 31 個案例中選中 5 次，全部正確：
    - sphinx-9367 ↔ tuple task skill；
    - fix-git／git-leak-recovery 各 2 條軌跡 ↔ reflog task skill，這幾條在舊門檻 0.45 下都不會注入。
  - 沒有誤注入。
  - 漏掉 4 次：
    - build-pmars 兩條與 polyglot-c-py 一條，對「無 root 從原始碼編譯 C」；
    - sphinx-8265 一條，對 tuple skill。
  - 正式規則下沒有相關的 task skill，Jev 全部判「不適用」，也沒有誤注入。

## 4. 接線與環境（offline 或零模型呼叫）

- 09-09～09-10：OpenClaw 原生 compaction 與 plugin 接線，使用 fake transport。
  位置：`evidence/implementation/`（僅存 T2）。官方 OpenClaw 2026.9.3 已驗證 task 搬移、
  event 退休與重新注入。
- 09-13：C-only runtime parity 稽核（`docs/evidence/r015-c-only-runtime-parity-20260913-03.json`）、
  公開題目稽核、執行時間估計（`docs/evidence/`）。
- 09-16：Harbor 執行使用者修正與三題環境 probe
  （`docs/evidence/r015-environment-alignment-20260916.json`）。

## 5. 來源軌跡

- 9/4 歷史 TB2 baseline 與 Spine：
  `~/ray/openclaw/docs/plan/summary-spine/terminal-bench-results/raw/dsv4-v1d-core12-paired-r1-20260904-{baseline,spineB}/`
  - 兩組各 12 題；各有 11 條完整 session。polyglot-c-py 兩組都是 setup timeout。
  - code-from-image 含圖片，文字 manager 不可直接使用。
  - 盤點紀錄：`evidence/prior-traces/audit-20260905.json`（僅存 T2）。
  - 要讀 `openclaw.session.jsonl`：ATIF 的 `trajectory.json` 會把 assistant 訊息換成 placeholder。
- 正規化後的 10 條文字 baseline：
  `~/ray/codeskill-rebuild-20260905/runs/m2-r014-source-import-20260911-01/trajectories/normalized/`
