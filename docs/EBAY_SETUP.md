# eBay 価格検索のセットアップ手順

アプリの「eBay」ボタンは、eBay 公式の **Browse API** で出品中の価格を検索します。
使うには eBay Developers Program に登録し、**Production（本番）用のキー**を Render に設定します。

- 料金: 無料（無料枠で 1日 5,000 回まで）
- 必要なもの: メールアドレス（できれば独自ドメインの業務用）、Render の管理画面

> 画面の文言は 2026年10月時点のものです。eBay 側で変わることがあります。

---

## 1. 開発者アカウントを作る

1. <https://developer.ebay.com/signin?tab=register> を開く（「Register」タブ）
2. 次を入力する
   | 項目 | 入れる内容 |
   |---|---|
   | **Username** | 新しいユーザー名（普段の eBay の買い物アカウントとは**別**のものとして作られます） |
   | **Password** | パスワード |
   | **Email (for sign-in and password reset)** | メールアドレス |
   | **Re-enter Email** | 同じメールアドレスをもう一度 |
3. **I have read and accept the eBay API License Agreement** にチェック
4. ロボット確認があれば済ませて **Join** を押す
5. 届いたメールのリンクでメールアドレスを確認する
6. **承認まで約1営業日** 待つ（eBay の審査）

### 以前「弾かれた」場合に見直す点

eBay は理由を詳しく公表していないため、確実な原因は分かりません。公式の案内と、よくある例から見直す点です。

- **メールアドレス**: eBay 公式の案内に「*Please use a business email address to register the developer account.*（業務用のメールアドレスで登録してください）」とあります。Gmail などのフリーメールで審査に通らないことがあるので、**独自ドメインのメールアドレス**（お店・事業用など）があればそちらを使ってください。
- **VPN・海外経由の接続を切る**: 普段の日本の回線から登録してください。
- **同じメールで二重登録しない**: 以前の申請が残っている場合は、そのアカウントでサインインできるか先に試してください。
- **入力内容を正確に**: 後で追加情報（氏名・住所など）を求められたら、実在の情報を正確に入れてください。
- 再度弾かれた場合は、開発者サイトの **Help / Support** から問い合わせると理由を確認できることがあります。

## 2. アプリ用のキー（Keyset）を作る

1. 承認後、developer.ebay.com にサインインし **Application Keys** ページを開く（<https://developer.ebay.com/my/keys>）
2. アプリ名（例: `figure-inventory`）を入力し、**Production** 側の **Create a keyset** を押す
   - Sandbox（テスト用）は不要です
3. 作られたキーのうち、次の2つを使います
   - **App ID (Client ID)**
   - **Cert ID (Client Secret)** … **他人に見せない・GitHub に書かない**
   - Dev ID はこのアプリでは使いません

## 3. 「アカウント削除通知」の免除手続き（必須）

Production のキーは、この手続きをするまで **使えない状態（Your Keyset is currently disabled）** になっています。
このアプリは eBay の利用者データを保存しないので、**「保存していない」として免除**を申請します。

1. **Application Keys** ページで、App ID の横の **Notifications** リンクを押す（**Alerts and Notifications** ページが開く）
2. **Event Notification Delivery Method** の中の **Marketplace Account Deletion** を選ぶ
3. **Not persisting eBay data** のスイッチを **On** にする → 確認画面で **Confirm**
4. **Exemption reason** の中から当てはまる理由を選ぶ
   - 「eBay の利用者データを保存しない」旨の選択肢を選びます（このアプリは検索結果の価格を表示するだけで、出品者などの利用者情報を保存しません）
   - **Additional information** に書く場合の例: `This app only displays active listing prices returned by the Browse API. It does not store any eBay user data.`
5. **Submit** → 完了画面が出れば OK

> 正しくない理由を選ぶと、アカウント停止などの対象になると eBay は案内しています。上の内容はこのアプリの実際の動きどおりです。

## 4. Render にキーを設定する

1. Render のダッシュボードでこのアプリのサービスを開く
2. **Environment** を開き、**Add Environment Variable** で次の2つを追加
   | Key | Value |
   |---|---|
   | `EBAY_CLIENT_ID` | 手順2の **App ID (Client ID)** |
   | `EBAY_CLIENT_SECRET` | 手順2の **Cert ID (Client Secret)** |
3. **Save Changes**（自動で再デプロイされます）

## 5. 動作確認

1. アプリで商品をスキャンし、持込リスト（または保管在庫）の行の **eBay** ボタンを押す
2. 価格一覧が出れば完了です

| 表示 | 意味・対処 |
|---|---|
| eBay APIのキーが設定されていません | 手順4の環境変数が入っていない／名前の打ち間違い |
| eBayの認証に失敗しました（HTTP 401 など） | キーの貼り間違い、または手順3の免除手続きが未完了 |
| このJANで登録された出品は見つかりませんでした | 出品者がJANを登録していない。「英語キーワード」で検索 |
| 1日の利用上限に達しました | 無料枠（1日5,000回）を超えた。翌日まで待つ |

## 使い方のメモ

- **JANで検索**: 出品者が JAN（EAN）を登録している出品だけが出ます。確実に同じ商品ですが、出ないことも多いです。
- **英語キーワード**: 「Sabito Vibration Stars figure」のように英語で検索します（eBay は英語の出品が中心）。
- **日本からの出品だけ**: 日本の出品者の値付けを見るときに使います。
- 表示されるのは **出品中の価格（送料別・米ドル）** です。**売れた価格**は API では取得できない（Marketplace Insights API は審査制）ため、「eBayで売れた価格を見る」リンクから eBay のサイトで確認してください。
- 同じ検索は1時間キャッシュするので、何度押しても回数を無駄に使いません。
