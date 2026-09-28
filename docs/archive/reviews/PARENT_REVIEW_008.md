# 主 agent 候選品質審查 008（部分）

範圍：R009的8個可解析generate候選，包含3個通過sidecar結構檢查與5個被拒候選。另1個skip、1個length結果不冒充完整候選。尚無task候選，**未滿足D10要求的至少10個且含兩类人工對照**；不報品質通過率或效果。

來源：T2 `runs/m2-common-bank-r009-20260907-01/` 的 `extraction/event/` 與 `model_calls/*/response.json`；原始角色/順序取 `runs/m2-full-pool-20260906-01/trajectories/normalized/`，不是由模型自評推論。

| Source | 人工觀察與可定位證據 | 本輪判定 |
|---|---|---|
| fix-git | 55fc5179衝突後，a14624e9檢視、7057d86a修改、20404790繼續，92f2ee24回覆成功。比先前整題recovery skill更局部；尚未證明跨題有效 | 局部事件與動作引用有支持 |
| git-leak-recovery | 候選引用3bbeb29e觸發、80ef186c處理、78919eaf/d15ec9f6驗證。規則包含刪除所有unreachable objects，適用條件未完整說明其他物件是否也允許刪除；不能把原題授權泛化成任意repo操作 | 保存泛化/適用範圍限制，不手改候選 |
| kv-store-grpc | dcf37fa4觸發，212c40f3/bebb22da查生成檔，5cf6b492/dd0fa200後續處理；技能聚焦protobuf命名轉換。官方reward0，局部步驟可成功但不代表整題成功 | 局部支持與最終失敗分開 |
| cobol-modernization | trigger67a8090e為assistant index4；response032b0dbc為assistant index51；outcome5323eb04為toolResult index58（多檔byte-identical測試）。需找真實觀察trigger。把所有非digit轉0比「space padding」更廣，不能只由空白案例推論所有非数字正確 | sidecar拒絕有效；泛化仍需證據 |
| fix-ocaml-gc | triggers為toolResult indices75/78/79/83；response bf800bce為toolResult index85（替換成功），不是assistant動作。46a3aa9e/0dd94e83為build/tests結果。skill保留wh/Whsize_hd(hd)等來源變數，違反paper泛化要求 | 可嘗試引用修復，但不能藉修復改skill；maintenance需面對原候選品質 |
| headless-terminal | trigger與response同為0247a645（assistant index8）；db00542c為修改成功，bbe457df為互動測試輸出。現有sidecar沒有獨立的前置觀察；不能把同一動作當因與果 | 需原trace支持的局部trigger，否則持續invalid |
| pypi-server | trigger c6d9be26為assistant index34；43ae273d為後續assistant index36，9719fd52/fa59b0ef為服務/安裝輸出。3條rules只有1條rule_evidence，除了trigger還有覆蓋不完整 | 修復後仍須完整validator，不接受只改首個錯誤 |
| schemelike-metacircular-eval | trigger70317345/39516ce3在indices110/111，response ac1309b3為toolResult index116；outcome7ad0f5b6卻在index100，是較早的parse成功，不能支持之後修正。最終reward0 | 因果引用錯置；只可修復有原文支持的引用，不將早期成功移到後期 |

以上8個候選都保留原始內容。R011只允許限定的引用修復，不用人工改寫或刪除低品質候選美化結果；修復通過結構不等於品質有效。校準前全部10個no_group理由另保留於group_selection；不得把它們當task抽取的skip。
