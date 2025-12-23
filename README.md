# -# モバイルバッテリー貸し出しシステム

本リポジトリは、Practice #10 における
Web3層構造を用いたモバイルバッテリー貸し出しシステムの
設計資料を管理する。

## メンバー
2442025 菊地亜由美

2442033　今野早耶佳

2442037　佐藤心優

2442085　森本真理子


## 内容
- システム構成図
- UIモック
- ER図
- DB設計（SQL）


## UIモック
ログイン画面

メールアドレス入力
パスワード入力
ログインボタン
新規登録リンク

ホーム画面

現在地スタンド表示
バッテリー残数
検索ボタン
残高表示

スタンド検索画面

一覧表示
スタンド選択
詳細ボタン

貸出確認画面

バッテリーID
料金目安
貸出ボタン

返却画面

返却スタンド選択
返却ボタン

利用履歴画面

利用日時
利用時間
料金

##  ER図
erDiagram
    USERS ||--o{ RENTALS : rents
    BATTERIES ||--o{ RENTALS : used_in
    STATIONS ||--o{ BATTERIES : owns

    USERS {
        int id PK
        string email
        string password
        int balance
        datetime created_at
    }

    STATIONS {
        int id PK
        string name
        string location
    }

    BATTERIES {
        int id PK
        int station_id FK
        string status
    }

    RENTALS {
        int id PK
        int user_id FK
        int battery_id FK
        datetime rent_time
        datetime return_time
        int fee
    }

