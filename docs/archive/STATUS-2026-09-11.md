# CODESKILL 重建狀態

## R014 offline correction acceptance (2026-09-11)

The parent independently verified code checkpoint `a7579a7` from an isolated Git archive: **142/142 tests passed in 12.736 seconds**, using `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8`. The earlier parent run with mixed UTF-8/cp950 subprocess decoding had three infrastructure errors; its log is preserved and does not establish a code failure or pass.

Accepted in this offline milestone:
- Native and legacy compaction controls are recognized; complete original control objects and their hashes are retained without promoting summaries to observed source evidence.
- Explicit unlimited development-ledger activation preserves existing history and cannot be downgraded by a legacy finite client. An independent reservation-only replay preserved 74 historical entries and reached 109 entries under an old 100-call profile, with zero network/model calls.
- The actual R012 entrypoint handles below/equal/above input limits, multiple action-observation segments, exact final-request hashes, and rejection of citations to omitted raw fragments.
- Overflow and tokenizer unavailability after successful summary calls retain the phase and prior call count; no final extraction completion is sent when its preflight is blocked.

Parent evidence: `validation/r014-parent-a7579a7-utf8.log` and `validation/r014-parent-a7579a7-original-repros.json`. These are private local review artifacts. The tracked full-suite log is `validation/r014-final-corrections-full-test-20260911.log`.

This does not establish real-model extraction quality, the complete solver/evolution/maintenance loop, or A01-A19 completion. No new real-model call or formal 72-trial campaign was run for this review. The new public GitHub repository is authorized; publication is pending sanitized-snapshot review and push verification.

## R014 全面驗收盤點（以已驗收 328e350 為基準）

下表逐條對照正式契約，不以 130 項測試代替整體完成。證據欄的測試名稱是離線證據；歷史 live 結果沿用原標籤，不能當成 R014 新實跑。所有項目維持原需求，沒有核定縮水或偏離；已知缺口列於最後欄。

| ID | 目前交付／證據層級 | 缺口與下一個驗收條件 |
|---|---|---|
| A01 | 配對／task 輸入驗證有離線測試；歷史 R011 全 no_related_group | 尚無 2–3 條真實相關軌跡成功 task 抽取；不強迫配對，不把 fixture 當成功 |
| A02 | 描述、引用驗證與兩條 Git trace 人工抽查已有證據 | 全來源描述／原文與 fallback 後引用鏈仍需核對 |
| A03 | 三次探索、skip／重複停止、prior candidates 與 retry 分離已有離線測試 | 尚未完成 R012 真實 manager 多次抽取與 bank 成長結果 |
| A04 | Actual R012 fallback entrypoint passed offline below/equal/above-boundary, multiple-segment and blocked-final tests at a7579a7 | Real-model fallback and extraction quality remain unverified |
| A05 | schema、非法候選、維護 target 驗證與 bank 保存有離線測試；有歷史 event 入庫 | 新 fallback 候選須走相同完整驗證，不繞過 invalid／原文證據檢查 |
| A06 | supplied-only、transport retry、退休 event、evolve→maintenance 有離線測試 | 缺真實本題 injection→evolution→maintenance→下一題 bank 完整鏈 |
| A07 | add／merge／drop／idempotence 有離線測試；歷史 C maintenance 為 add | 未自然出現的 live merge/drop 明示未觀察，不能假造成功 |
| A08 | MiniLM 固定 revision 介面與欄位 token 配置有測試；歷史 embedding 已使用 | 新 sidecar 的真 MiniLM 檢索→真 solver 接線尚未聯合驗收 |
| A09 | 直接／祖先／canonical 同題／未來來源排除有離線測試 | 完整多組 runner 的實際 snapshot/provenance 仍需逐 trial 核對 |
| A10 | frozen-bank 入口與初始注入有離線測試；官方 CLI native probe 使用指定 fixture skill | 缺真 solver 初始請求中的真檢索技能與 no-skill 組反例 |
| A11 | 官方 2026.9.3 fake-only 實測 task carry、event retire/reinject；失敗／取消修正有離線測試 | 缺真 observation→相關檢索→下一次 solver 決策；失敗／取消 native 場景未逐種實跑 |
| A12 | durable freeze/release、失敗不更新 bank、阻止 replay 有離線測試 | 完整跨程序中斷／恢復與原始 run 證據鏈仍需整合驗證 |
| A13 | 同題全部 arms/repeats 凍結有離線測試；注入開關與演化分離 | 真 runner 三組相同條件、獨立容器/workspace 與重複試驗尚未驗收 |
| A14 | 尚未交付官方解題結果 | Harbor adapter／solver／official verifier 要真正執行並保留原始結果；infra 與模型失敗分開 |
| A15 | 正式開跑確認規則已寫入文件；未啟動 72 trials | 完成前置、凍結設定並取得使用者確認後才能執行，不能提前標通過 |
| A16 | 既有紀錄／指標程式需按最新 runner 再核對 | 缺完整新 run 的 raw-record 指標重算與分母驗證 |
| A17 | CODESKILL 獨立 repo／新核心；R013 plugin 在官方未修改套件上實跑 | 最終 runner 依賴與歷史資料引用再審，不能使用舊核心或舊結果冒充新驗收 |
| A18 | Explicit unlimited development policy implemented and independently tested; method/per-call limits retained | Remote live-ledger activation and final formal profile/thresholds remain pending; do not infer approval |
| A19 | Native/legacy compact controls and original control hashes preserved; omitted-raw-citation rejection verified through actual offline entrypoint | Real historical compacted trace to real extraction with verified original citations remains pending |

基準證據：`validation/r013-packaging-full-test-20260910.log`（130 項）；T2 `evidence/implementation/r013-parent-remote-130.log`；`evidence/implementation/r013-parent-official-openclaw-2026.9.3-final/status.json`。歷史 R011 與 stress 原始證據位置仍見下方原狀態段落。

R014 first offline milestone was completed by the resumed Terra/high agent and independently accepted by the parent at a7579a7. The acceptance above supersedes the earlier pending-delivery note; the broader A01-A19 gaps and formal-run approval gate remain unchanged.

更新：2026-09-10，文件對齊 v0.12／R012／R013。R013 獨立 OpenClaw 接線與凍結 bank 入口工程階段已驗收；真實模型／官方試驗與完整研究驗收尚未完成。下方歷史段落保留各自當時的證據。

| 階段 | 狀態 | 證據 / 缺少條件 |
|---|---|---|
| M0 現行契約 | requirements_approved_v0.11 | 已記錄 R012 使用者決策、派工與開跑關卡；歷史基線保留；不代表程式驗收完成 |
| M1 環境與服務 | in_progress_transport_pass | 主 agent 回讀短 chat probe：HTTP 200、43 input / 12 output tokens、stop；真 MiniLM 已載入且主 agent 核對長欄位256 tokens；DeepSeek /tokenize 與9個真M2請求usage一致；新harness/verifier與完整M1條件仍待驗證 |
| M2 離線 skill 流程 | live_partial | R011-02：10 份描述、0 task group、6 個 event 候選與 6 次維護；失敗及品質限制保留。R012 的多次抽取與僅演化已提供技能待實作／驗證 |
| M3 Agent 實測閉環 | implementation_pending_R012 | 原壓縮／代理接線尚未完成；R012 的 task 保留、event 移除／重新注入與多條匹配事件尚未實測通過 |
| M4 正式比較 | not_started_user_gate | 12 × 3 × 2＝72 次試驗；題目分割保留，最終設定待定；完整說明功能並取得使用者明確確認後才可開跑 |
| M5 最終驗收 | not_started | A01–A19 尚未通過本版完整驗收 |

## 已执行

- 唯讀核對原論文 §3、§4、附錄 A–E 及 Figures 6–14；來源 PDF hash 寫入規格。
- 唯讀確認 T2 父目錄與基本 command availability。
- 在 T2 新建 `<PROJECT_ROOT>/docs/`。
- 撰寫原文對照、D/V/U、固定範圍與驗收契約；沒有讀取或接續舊半成品。
- 依使用者補充要求，查閱先前 session 的連線線索並從 T2 live 核對 <MODEL_SERVICE_HOST> 的模型 metadata；寫入本專案 `configs/model-endpoints.json` 與 `evidence/connection/metadata-20260905.json`。沒有複製舊程式或舊 run，沒有複製金鑰。
- 完成 T2 文件回讀 SHA-256 比對、17 條論文對照與 18 條驗收條目檢查，保存實作前 v0.2 固定副本；此項只驗證文件。
- 依使用者新指示唯讀盤點昨日 baseline/Spine：11 對 raw sessions 有完整動作／結果配對與 verifier；一對 setup timeout 無 trace；code-from-image 保留 image blocks，列多模態待處理。來源檔未修改，未送推論。
- 保存 `evidence/prior-traces/audit-20260905.json` 及 `docs/TRACE_REUSE.md`，規格升至 v0.3：先重建核心、重用 baseline source，Spine 抽取比較延後。歷史資料不取代本版 live 驗收。

## 當前可說的結論

現行契約為 v0.11／R012。歷史 M2 與 tokenizer 證據僅支持當時記錄的範圍／版本。新 event 生命週期、抽取與演化規則目前是需求，不是已實作結果；尚無完整實測閉環或效果結論。本次文件更新未啟動新代理或實驗。

短 probe：runs/m1-manager-shortprobe-20260905-01/model_calls/call-0001/response.json；SHA-256 0a1a9b5d4fc6aa6b824d19221a7c49ece96b76afa475c3b323e5f7fefb275b5c。


## 主 agent 開發中審查

- docs/PARENT_REVIEW_001.md 保存首輪反例與靜態問題；Terra 修正後，主 agent 在 T2 重跑完整20項核心單測通過。
- evidence/parent-review/review-002/verification.json 與 unittest.log 保存回驗；原bank snapshot、hash與來源排除反例均通過。這不代表完整A01–A19通過。
- evidence/parent-review/review-002/minilm-long-fields.json：真MiniLM tokenizer對合成長欄位測試，最終256tokens、三欄保留、實際encoder input IDs一致；不是抽取／注入效果。
- evidence/dataset/split-r004-frozen.json：官方commit與靜態資格、分割及hash已保存；尚未pull／執行正式task containers。


## 真 M2 與已記錄缺口

- runs/m2-offline-pilot-20260905-01：4個描述、配對no_related_group、event抽取與maintenance add；D04補正後局部trigger和step sidecar存在，仍有粒度/規則品質限制，見docs/PARENT_REVIEW_003.md。
- evidence/parent-review/review-003/pilot-token-counts.json：主 agent核對9個completion，exact preflight全部等於reported prompt tokens、HTTP200/stop。
- runs/m2-context-stress-20260905-01：3摘要後final 21090 > 11712，正確停止；沒有final extraction，不能標fallback通過。R006核定獨立受限重試。
- R005允許既定10text來源與累計60generation calls開發上限；formal split不變。

## 2026-09-06 接續驗收與阻礙

- 主 agent 在 T2 獨立執行完整 unittest discover：37/37 通過，exit 0；仍非 A01–A19 全部通過。
- runs/m2-context-stress-20260906-04：原始20304、投影完整12251均超過11712 input上限；壓縮後8259，真completion usage也是8259、stop，產生event。重用兩個有效摘要，沒有使用截斷摘要。只接受上下文機制transport證據。
- 該event的衝突處理規則引用衝突結果與最終檔案狀態，未直接引用處理動作；品質仍有限，不能由JSON成功或citation存在推論skill有效。詳見docs/PARENT_REVIEW_004.md。
- 自動核准審查拒絕10條baseline trace傳送至<MODEL_SERVICE_HOST> DeepSeek端點，理由為潛在敏感內容外送須明確指定資料/端點的授權。已向使用者說明並詢問；未重試，未改路徑繞過。Terra繼續R008離線實作與測試。

## 2026-09-07 續行

- 使用者在收到指定10條baseline資料、DeepSeek端點與auto-review風險說明後回覆「繼續完成」。主 agent依此授權Terra重新提交原動作給審查，仍不得繞過拒絕。
- Terra前次因Codex usage limit中斷，本輪恢復同一Terra xhigh代理，未切换模型規避限制。開發ledger live回讀仍18/60。
- R008最初overlay程式尚未完成驗收。主 agent已指出當前anchor位置、無命中batch重評、錯誤紀錄覆寫、tools schema token計量與compaction relocation等邊界，待Terra修正及回驗。
- Terra修正projection及overlay v2後，主 agent在T2完整47/47單測通過；另測出一次合法compaction後，下一個普通請求錯誤要求新的compaction證據。已交修正。證據：evidence/parent-review/review-005/unittest.log、verification.json；屬離線fixture，不是live。
- 重新提交10-source傳送仍遭auto-review拒絕：審查不接受「繼續完成」作為指定資料/端點直接授權，並表示工具justification中的授權說明不足。主 agent已再次要求使用者明確文字授權；沒有重試、繞過或新增模型calls，ledger仍18/60。離線實作繼續。
- 隨後使用者在本task直接回覆：「同意將這 10 條 baseline trace 與衍生內容傳送到 <MODEL_SERVICE_HOST> 的 `http://<MODEL_SERVICE_HOST>:31000/v1`，用於 CODESKILL 實驗」。已轉達Terra依新授權重新提交原動作；核准與執行結果另記，不以授權代替執行。
- 原10-source動作獲auto-review核准並完成，run為runs/m2-full-descriptions-20260907-01。10份描述，10次HTTP200/stop；input6331–161609，exact preflight均等於usage。主agent驗證source hash與citation IDs，保存evidence/parent-review/review-005/full-source-descriptions.json。這不是全面語義品質審查。
- R009固定全部10anchor依字典序配對、同source group去重task、每source一致一次event及逐候選真maintenance；累計開發上限100，stress仍12。28次已用，不重設ledger。

## Git及最新審查

- 使用者要求Git版本控制，已在T2專案建立codex/rebuild-v1：0aa2950保留中斷實作/v0.8基線，6ea07f9保存R010/v0.9，701b224保存R011/v0.10。無remote/push。詳docs/VERSION_CONTROL.md。
- 程式、測試、prompt、企畫書與artifact references納入Git；raw runs/evidence、live endpoint設定、ledger、cache/venv排除。artifact hashes是定位證據，不是將raw資料備份到Git。
- R009實際run：runs/m2-common-bank-r009-20260907-01；23 calls，全部10pairing no_related_group，3event generate並add，6錯誤（1length及5sidecar局部性/時序），1skip。主agent已回讀run-status與所有pairing理由/三個候選。開發ledger live回讀51/100。
- evidence/parent-review/review-006保存主agent完整52/52單測及compaction後連續兩request仍carry一次、恢復後不明anchor消失正確拒絕。此為offline fixture。
- R010第一次衍生run：runs/m2-r010-arm-bank-derivation-20260907-01。Terra報54/54、3/3maintenance messages匹配、0新calls；主agent審查發現新C bank重編operation/skill ID卻用舊pre-bank做匹配證明，且dirty patch漏untracked新程式。已交修正為保留原C bank身份的獨立副本，加完整code snapshot；-01保留，未驗收。
- R011尚未執行；無M3/M4官方trial，不將3個event或排程completed宣稱第一版完成。
- R010修正版runs/m2-r010-arm-bank-derivation-20260907-02經主agent驗收：C檔案bytes/hash等於原bank、B/C獨立檔案、三筆maintenance輸入相同且取消filter沒有移除候選；0新模型calls。完整source snapshot含untracked新程式，dirty狀態明示。主agent重跑54/54通過，證據evidence/parent-review/review-007；只通過本次離線分庫修正，不代表M2全驗收。

## 2026-09-08 服務恢復（啟動時快照）

- R011程式已由主agent56/56單測回驗並提交e1ac5b1。首次runs/m2-r011-calibration-20260908-01的19個preflight均tokenizer connection refused，無模型reservation/completion，ledger仍51/100。不能把0group解讀為模型no_group；該run是infra阻擋。
- 使用者明確要求重啟<MODEL_SERVICE_HOST>並參考舊設定。主agent查得舊Slurm job448因三日時限TIMEOUT，非主機當機。新提交前user jobs為0、4GPU各0MiB、quota251G/500G、兩模型埠未監聽。
- 依原SubmitLine重提scripts/dsv4-9b-tb21-server.sbatch，參數--export=ALL,MFS_MAIN=0.60,SUM_TARGET_GB=24，取得job458。腳本SHA256 45834ef86b197decc54622b196d28f9ddf05404750e547f2b54f87644ae35668；<MODEL_SERVICE_HOST>原工作目錄<REMOTE_USER_HOME>/deepseek-3fs。
- 最後直接觀察job458 RUNNING，DeepSeek進入DeepGEMM warmup。沒有重開主機，沒有終止其他工作；尚未確認DeepSeek/Qwen兩端API ready或新completion。
- 啟動證據已在T2 evidence/connection/restart-job458-20260908.json。之後Terra遠端唯讀檢查遭auto-review以Codex usage limit拒絕（提示23:27），停止重試/替代路徑；此處新增狀態尚未同步或commit。
- 下一步：用正常恢復的工具檢查458狀態、兩端health/models、DeepSeektokenizer與metadata一致性；ready後按同R011契約新run恢復（第一次未有模型decision），保存新server identity及原infra失敗。不得僅因Slurm RUNNING標服務恢復或實驗完成。

## 2026-09-08 用量恢復後實驗與主代理驗收

- 使用者要求同一 Terra xhigh 繼續。R011-02 已完成，主代理親讀 22 個 HTTP200/stop 回應及 token comparison 全相等；模型服務已實際回應，最大輸入290326。前次 -01 infra 失敗不覆寫。
- 新 run 使用 clean e1ac5b1 及 v0.10/R011 完整快照。10 個 pairing 均 no_related_group；6 event 候選、B直接入庫、C六次真maintenance均add；兩筆修復失敗、一次cannot_repair及一次skip保留。ledger Terra live回讀73/100。
- 主代理獨立核對 snapshot、candidate references、B/C內容與來源，見 PARENT_REVIEW_010.md 及 evidence/parent-review/review-010/r011-verification-v2.json。語義品質限制與零task缺口仍存在，不宣稱M2整體完成。
- Terra 接續 M3 前置與已核定 A/B 開發試跑；主代理維護研究方向、文件及Git。主代理61/61只驗當時離線修正，尚無本輪官方solver/verifier結果。

## 2026-09-09 M3 前置進展

- 服務 readiness 新證據：evidence/connection/readiness-job458-20260908.json；DeepSeek health/models HTTP200，context524288/max_req_input524282，auto-truncate=false。舊啟動快照維持原樣。
- 主代理已回驗期限/上限修正62/62，保存bae4623；新增完整payload計量後65/65，保存b3c2b60。以上是離線測試，不是官方trial。
- runs/m3-full-payload-tokenizer-20260909-01：含tools/tool_choice/reasoning的合成payload，preflight323等於HTTP200 completion usage323，finish_reason=tool_calls，output119，工具未執行。主代理亲讀raw response；ledger74/100。此為計量前置證據，非OpenClaw實際接線或skill效果。
- 正在驗證native compaction事件與proxy request的關聯。主代理查到原生appendCompaction/session_compact及session compact:after包含更完整session/entry資料，已交Terra驗證實際路徑；未放寬M3啟動條件。

## 2026-09-09 — 使用者決策已記錄，程式待對齊

- 更新 REPRODUCTION_SPEC v0.11、DELEGATION，並追加 R012。歷史決策、基線、原始實驗及既有未提交程式維持原樣。
- 已確認行為：確認壓縮後只搬移保留 task；停止補回 event 明確區塊，允許後續相關事件重新注入；每條軌跡至多三次 event 抽取且可提前停止；多條匹配且不重複的 event 注入；只演化本題提供過的技能；同題重複隔離、跨題學習維持防洩漏。
- 開發期間須檢查短描述是否漏掉共通多步流程；具體規則見企畫書 D02 及 R012 第 2 點。目前尚無這項檢查的新結果。確切選取上限、相關性規則、token 預算及額外相關性判斷模型仍待決定，不自行定案。
- 方法修改須先與使用者討論並確認，Terra 建議也適用。保留一位 Terra xhigh，以完整功能為單位精簡交接，主代理獨立驗收。英文用於推理、程式碼與向主代理的工程回報；對使用者溝通及供其閱讀的專案文件使用繁體中文。
- 正式 72 次試驗前，完整說明 CODESKILL 所有功能、已驗證行為、限制與設定，等待使用者明確確認。
- 本次僅更新文件：沒有修改程式、新跑測試、呼叫模型、啟動 subagent 或正式試驗。歷史 75／75 離線測試不能認證 R012；受影響的驗收仍待完成。

## 2026-09-09 — R012 實作啟動與來源配對抽查

- 使用者明確授權開始實作。已派一位 gpt-5.6-terra／xhigh，先完成 R012 程式與完整受影響離線測試，主代理獨立驗收後再評估受控整合；本次尚未啟動模型或官方試驗。
- 配對抽查已有局部證據：fix-git 原始步驟 2c782b31→e9105fe9，以及 git-leak-recovery 的 e5277f5c→5c1c3143，均有 reflog 尋找孤立提交後以 git show 檢查的流程。兩份短描述各省略其中一部分；R011 拒絕理由將兩者區分為 reflog 與 show。
- 證據：evidence/parent-review/r012-pairing-description-audit-20260909/audit.json，SHA-256 1c9eb7dcaa86c845cd87b74220aa9884b3799b296f664fc8219018886dc8f3ec。已核對來源 hash；沒有修改描述、配對規則或技能庫，也沒有新增模型呼叫。
- 此抽查僅涵蓋歷史 R011 已指出的兩條 Git 來源，支持描述遺漏疑慮，不代表全部配對皆如此，也不證明重新配對會成功或能抽出合格 task skill。方法若需調整，先與使用者討論。
- 舊 M3 開發／受控測試文件已加入 R012 優先適用提示；新檢索門檻、數量及 token 預算仍未定案，不按舊文件直接啟動試驗。

## 2026-09-10 — R013 架構決策與主代理驗收狀態

- 正式納入 R013：獨立 CODESKILL repo、僅支援 OpenClaw、OpenClaw 原始碼與執行產物零修改；Hermes 延後。plugin README 先前已有部分說明，本次補入決策、契約與派工文件，並將隔離分支舊規格對齊原專案已核定的 R012。
- 隔離分支 codex/r012-sqlite-native-compaction 的 87bebfc033b21b7e72a770d43f78319eb40d99d7 實作公開 plugin 與 sidecar；尚未合併到原工作樹。主代理重跑完整 110/110，1.868 秒，見 evidence/implementation/r012-parent-public-sidecar-acceptance-20260910.log。
- plugin-run-20260910-05 原始 status：真 CLI、fake transport、6 proxy/6 upstream、52.65 秒、0 real model calls；SQLite compaction 95821e19、摘要 parsed request 原樣轉送、task relocation 與 event retire 有證據。尚未以此證明 event 重新注入、官方 solver/verifier 或正式試驗成效。
- 主代理未接受完整交付：摘要取消／失敗／無新 SQLite row 後，active permit 仍可能放行下一個一般請求，跳過 overlay。gate 重現證據為 evidence/implementation/r012-parent-public-sidecar-lifecycle-gap-20260910.json。另缺可直接執行的獨立啟動入口與一般官方 OpenClaw 版本相容性驗證。
- 已交回同一 gpt-5.6-terra／xhigh 修正；代理因用量限制中斷，工具提示可於 18:03 再試。目前修正版尚未交付或驗收，不把 110 項既有測試當成缺口已修復。
- 本輪查得共用 OpenClaw 與未採用的 purpose-marker worktree 都仍在 a28960df22a9b8aed5f37c393c3998b643a18a3d，git 工作樹乾淨。歷史 95f5ef44bf5 等 Summary-Spine 提交確實修改過 OpenClaw；零修改僅指本輪 CODESKILL 接線，不能宣稱該基準從未修改。
- 原專案既有程式變更保留；本次只更新主代理負責的四份文件，未啟動模型、GPU 或 72 次正式試驗。

## 2026-09-10 — a42251e 原生接線階段驗收與 high 接續

- Terra xhigh 提交 a42251ed8eb66e99646d033a5e63cecb4e1f3850，加入公開 provider 的 normal-call boundary；一般呼叫會撤銷未完成 permit，仍經 overlay，修正 87bebfc 的失敗／取消缺口。主代理親讀修改並重跑 117/117，2.000 秒，evidence/implementation/r013-parent-acceptance-a42251e.log。
- 主代理核對全新官方 npm openclaw@2026.9.3 安裝與原始 request artifacts，tarball SHA-256 d1c63366833f8ae4a6ab4f3b60b1aa84ca82d03dba13d3d3eba989aa159e2449；official-openclaw-2026.9.3-run-20260910-08 有 4/4 CLI exit 0、8/8 fake requests、16.37 秒、0 real model calls。attempt 5 摘要原樣通過；attempt 6 的真 SQLite compaction、task relocation 和 event retirement 相連；attempt 8 在新 anchor 重新注入同一 event skill。官方套件 read-only 執行，未用歷史 Summary-Spine 工作樹。
- 通過範圍僅是上述版本的原生接線與受控生命週期；skill 是受控 fixture，不能當成真實檢索命中、solver/verifier 或正式試驗結果。
- 尚待完成獨立使用入口：sidecar 目前的 taskSkill/eventSkill 是手動設定，需接入既有 bank/retrieval；README 尚缺啟用 codeskill-r012 provider 的完整模型設定。這些不得被省略或當作產品已可直接使用。
- 使用者授權主代理動態判斷 Terra effort。已在固定版本交接後，從已完成的 Terra xhigh 切換至一位 Terra high 接續上述明確入口／設定工作；同時只有一位 Terra 執行。主代理維持里程碑驗收，不輪詢未完成程式。

## 2026-09-10 — R013 獨立接線與入口階段完成

- 主代理驗收程式 commit `90a7fddd2f8ef458151bf16d2606231b84319ffd`。本機完整 130/130（6.912 秒）與 T2 完整 130/130（2.649 秒）通過；遠端 log：evidence/implementation/r013-parent-remote-130.log。
- 已有 CODESKILL 自帶 public OpenClaw plugin、per-session sidecar、可執行設定檢查／啟動入口、凍結 bank 與既有 MiniLM 檢索接線。設定檢查驗證 provider/plugin/trial/session/permit 路徑；手動 fixture 選取必須明示 test-only。
- 注入開關由 frozen profile.sidecar_injection.arms 明示，與 evolution.full_lifecycle_arms 分開；不因 B 不演化而禁止其注入。event budget 的工程支援範圍明示為 complete_payload_active_event_blocks_delta：每個一般請求計算全部 active event 區塊的完整 payload token 增量，超額停止，不截斷或挑掉部分技能。數值、門檻及正式 profile 仍未核定，程式不替使用者選定。
- 主代理重跑官方未修改 OpenClaw 2026.9.3，read-only npm dependency，8 HTTP／8 fake requests、0 real calls、16.12 秒；真 SQLite compaction、原樣摘要、task carry、event retire/reinject 均有原始證據。見 evidence/implementation/r013-parent-official-openclaw-2026.9.3-final/status.json 及 r013-parent-final-acceptance.json。
- 受控 native probe 的 skill 是 fixture；凍結 bank 檢索與不同 arm 行為另以離線測試驗收。尚未驗證端到端真實模型／Harbor solver／official verifier 或效果提升；正式 72 trials 未啟動。
- 程式已在 T2 隔離分支整合，原專案的既有程式 dirty worktree 保留，未覆寫；未向 GitHub 發布。此完成點是 R013 獨立接線／入口工程階段，不等於整個研究或 M3/M4 已完成。
