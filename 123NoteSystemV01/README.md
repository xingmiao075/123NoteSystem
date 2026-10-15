# 123NoteSystem V1

個人線上筆記系統的 Flask／SQLite MVP 原型。

## 功能

- 註冊、登入與使用者資料隔離
- 新帳號使用 Email 登入，既有帳號可繼續使用原使用者名稱
- 空白草稿與延遲自動儲存
- 已同步草稿永久保留，不自動到期
- 筆記版本歷史、內容檢視與還原；還原會新增版本並保留原歷史
- 筆記標籤、搜尋及排序
- 釘選、封存、垃圾桶、還原與永久刪除
- 圖片及文件附件上傳、圖片預覽、下載與移除（每檔上限 10 MB）
- 瀏覽器錄音、錄音預聽／重錄及保存為音訊附件
- 自動儲存失敗時暫存於目前瀏覽器；登出時清除本機草稿
- CSRF 防護、密碼雜湊與舊資料庫欄位遷移

## 本機執行

```powershell
..\.venv\Scripts\python.exe -m pip install -r requirements.txt
..\.venv\Scripts\python.exe app.py
```

開啟 http://127.0.0.1:5000。首次啟動會在 `data/notes.db` 建立 SQLite 資料庫；上傳附件保存在 `data/uploads/`，兩者均不應提交到版本庫。錄音需使用支援 MediaRecorder 的瀏覽器並允許麥克風權限；部署環境需使用 HTTPS。

## 測試

```powershell
..\.venv\Scripts\python.exe -m pytest
```

正式部署前必須設定高熵 `SECRET_KEY`、`APP_ENV=production`，並使用正式 WSGI 伺服器；不要以 Flask debug server 對外提供服務。Email 驗證與密碼重設、離線同步尚未實作。
