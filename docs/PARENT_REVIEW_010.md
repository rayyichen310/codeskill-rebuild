# 主代理審查 010：R011 恢復後結果

日期：2026-09-08。対象為 `runs/m2-r011-calibration-20260908-02`。接受固定排程與分庫執行證據；M2 品質驗收仍未完成，M3/M4 尚無本輪 solver 結果。

## 獨立驗證

- 起跑為 clean `e1ac5b1adb1e5373142cacfc6be76a480aebd8d7`，v0.10/R011。主代理核對完整 code snapshot、兩份 contract、六個 candidate references 的 SHA256。
- 22 個新 manager calls 全 HTTP200、stop、preflight prompt tokens 等於 usage；最大實測輸入 290326 tokens。不能據此宣稱 524K 全窗口或含 solver tools 的 tokenizer 已通過。
- 10 個 anchor 全 no_related_group，0 task extraction。6 個 event 候選，C 六次真 Fig.9 均 add。B/C 內容集合及來源相同，獨立檔案；B canonical-schema 排序，C 按候選順序維護。這次尚無 merge/drop 的 live 證據。
- 主代理初次錯用 B 物理儲存順序作斷言，失敗紀錄保留；讀碼釐清後改驗內容 multiset 及 provenance。證據：`evidence/parent-review/review-010/r011-verification.json`、`r011-verification-v2.json`。
- B bank SHA256 `b93c0db716baf2a4d06a7ac1c2cbf1fb7c1821dcd479975cc245f14633a54997`；C bank `360d8c56a00baf559b48439fa33c17ad4a5552934c9871f45cf2ed2f502ef269`。
- M3 proxy 初版修正另由主代理重跑 61/61，`evidence/parent-review/review-010/unittest.log`；只代表當時離線測試。prepare 後 deadline 重算及真 OpenClaw 接線仍交 Terra 繼續，不能用此測試取代新修改的回驗。

## 候選品質與缺口

本輪新增 build-pmars，修復 cobol 與 schemelike 的 sidecar；沿用 fix-git、git-leak-recovery、kv-store-grpc 三個候選。

| 來源 | 原始 trace 核對與限制 |
|---|---|
| build-pmars | b68d3ad8 是 deb-src 缺少錯誤；9d367870/7df67f48 查格式，372156ce 建立 sources；b40325c9 顯示 update 成功，e4691097 顯示取得源碼。局部流程有支持。rule 3 引用的是結果，沒有直接引用發出 update 的動作。 |
| cobol-modernization | d470812c 是 15-byte 資料觀察，032b0dbc 後續程式包含 padding/數值解析，5323eb04 是短輸入差分測試一致。引用順序已修復；「所有 non-digit 都變零且符合 GnuCOBOL」仍超出此處空白/短輸入的測試支持，不以 sidecar 合法宣稱普遍正確。 |
| schemelike-metacircular-eval | 70317345 是 let AttributeError；39516ce3 是公開診斷與 grep；ac1309b3 是一次 edit 成功。可支持診斷及改写發生，不能證明所有 named-let 都改對或 evaluator 最終成功；修復後仍未直接引用 edit assistant 動作。 |
| 其餘三個 | 沿用 PARENT_REVIEW_008 的品質限制；Git 清理規則範圍偏廣、gRPC 局部成功不等於官方題目成功。 |

fix-ocaml-gc 仍違反 response 必須是 trigger 後 assistant action；pypi-server 仍缺逐 rule evidence。headless-terminal 回 cannot_repair_evidence：來源只有自我發現，缺可用的外部局部 trigger。cancel-async-tasks 沿用 skip。四者全部保留，沒有人工補成成功。

## 下一步決策

依既定 R011 不再追調配對 prompt、不強制 Git 配對、不對失敗來源無限重抽。凍結六候選 B bank 可供已核定 M3 開發接線試跑，manifest 必須明列沒有 task skill 及以上品質缺口。這不代表 D10 的兩種粒度品質抽查達標，也不代表 M2 完成。

Terra 繼續既定 M3 profile 的 tools token 計量、overflow/compaction 真接線與期限限制，前置條件滿足後執行 password-recovery A 後 B。自然無命中記未觀察，不能改門檻或強塞 skill。方法變更仍由主代理決策。
