# 主 agent 接續審查 004

日期：2026-09-06。範圍：新專案投影、stress-04現有證據與完整單測，不呼叫模型。

## 實際驗證

主 agent 於 T2 執行 `PYTHONPATH=<project>/src .venv-manager/bin/python -m unittest discover -s tests -v`。37項全部通過，exit 0；包含bank版本/provenance、context阻擋、tool配對、ledger、格式錯誤、tokenizer計量與投影。不能用這些測試取代OpenClaw live注入或官方verifier。

回讀 `runs/m2-context-stress-20260906-04/extraction/event.json` 與 `run-status.json`：final生成event，狀態completed，明確標示無品質claim。前次核對的8259 input與usage一致，最終未超額。

## 品質限制

生成的git recovery event仍涵蓋尋找commit、cherry-pick、解衝突。rule 2引用55fc5179衝突與faabd02d最終狀態，未直接引用7057d86a/20404790的處理行動。這不表示規則必然錯誤，但sidecar尚不能直接支持每條動作，需在候選人工審查中記錄。不得手改skill或反覆重抽以取得偏好的敘述。

## 投影靜態缺口，已回報Terra

1. `_parsed_json_equal` 的 Python equality 將boolean與數字判等，例如true與1；嚴格JSON去重需保留型別差異。
2. `details.aggregated` 的represented_by固定為content[0].text；應指向實際唯一text block的index。
3. trace whitelist未保留control_events等非重複欄位。應保留控制與完整性metadata，或明確記錄非證據欄位的省略理由；不能將整個投影宣稱為僅刪嚴格重複。

以上為靜態邊界缺口；37項現有測試未覆蓋這些反例。修正後由主 agent重新驗證受影響行為，不重跑已有成功模型請求來覆蓋歷史證據。

## 執行限制

自動核准審查拒絕向 `http://<MODEL_SERVICE_HOST>:31000/v1` 傳送10條baseline trace與衍生內容，要求指定資料與端點的直接使用者授權。已詢問，依賴此傳送的描述/抽取暫停；離線proxy實作持續。這是approval限制，不是模型品質失敗，也不是pipeline skip。
