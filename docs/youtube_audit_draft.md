# YouTube API 審査（audit）申請の下書き

このツールの「YouTubeへ」「分析」で使っている YouTube API について、Google の審査（YouTube API Services の Audit）を申請するときの手順と、申請フォームに書く文の下書きです。

> **審査は必須ではありません。** 「YouTube Studio に動画をアップ → このツールで設定を反映」の流れなら、審査なしで今までどおり使えます。
> 審査が必要になるのは、次のどちらかのときです。
> - 「APIで動画ごとアップロード」を使いたい（審査前のプロジェクトからアップした動画は「非公開」にロックされます）
> - API を1日に使える量（初期値は 10,000 ユニット）が足りなくなった

`【 】` の所は、自分の情報に書き換えてください。フォームは英語なので、英語の文例を付けています（そのまま使えます）。

---

## 1. 申請の前に準備するもの

| 準備するもの | どこで・どうやって |
|---|---|
| Google Cloud のプロジェクト番号 | [Google Cloud コンソール](https://console.cloud.google.com/) の「ダッシュボード」→「プロジェクト番号」（数字） |
| 使っている API | 「APIとサービス」→「有効な API」で **YouTube Data API v3** と **YouTube Analytics API** が有効になっていること |
| OAuth 同意画面 | 公開ステータスを「本番環境」にする。アプリ名・サポートメール・**プライバシーポリシーのURL**を入れる |
| プライバシーポリシーのページ | 下の「4. プライバシーポリシーの文例」を、ブログや Google サイト・GitHub Pages などに載せて URL を用意する |
| 使っている様子の動画 | ツールで「YouTubeへ」→「設定を反映」、「分析」タブで分析するまでを画面録画する（3〜5分）。YouTube に**限定公開**でアップして URL を用意する（Windows は「Win + Alt + R」で録画できます） |
| チャンネルの URL | 自分のチャンネルの URL（`https://www.youtube.com/@【ハンドル】`） |

## 2. 申請の手順

1. 上の準備をすべて済ませる
2. [YouTube API Services - Audit and Quota Extension Form](https://support.google.com/youtube/contact/yt_api_form) を開く
3. 「審査（Audit）」または「割り当ての追加（Quota extension）」を選び、下の「3. フォームに書く文の下書き」を見ながら入力する
4. 送信すると、Google からメールで質問が来ることがあります。返事まで**数週間**かかることがあるので、気長に待ちます
5. 審査に通ったら、「YouTubeへ」画面の「APIで動画ごとアップロード」が使えるようになります（今までの非公開ロックは解除されません。新しくアップした動画から有効です）

> **OAuth の確認（アプリの確認）との違い**：Google には「OAuth 同意画面の確認（Verification）」という別の審査もあります。自分だけで使う場合は不要です（ログインのときに「このアプリは Google で確認されていません」と出ますが、「詳細」→「移動」で進めます）。

## 3. フォームに書く文の下書き

### 組織・連絡先（Organization / Contact）
- Organization name: 【自分の名前、または活動名】（個人の場合は個人名で問題ありません）
- Organization website: 【チャンネルの URL】
- Contact email: 【Google Cloud のプロジェクトのオーナーのメールアドレス】

### API クライアントの情報（API Client）
- Project number: 【プロジェクト番号】
- API client name: 【OAuth 同意画面のアプリ名（例: ai-video-editor）】
- Type: Desktop application (installed app on Windows), single user

### 使い方の説明（Use case）
日本語の要約：
> 個人の YouTuber が自分のチャンネルのために使う、Windows 用の動画編集ツールです。撮影素材から完成動画を作り、自分のチャンネルの動画にタイトル・概要欄・タグ・サムネイル・字幕を設定します。公開後は、自分の動画の視聴維持率とコメントを読み、次の動画の編集を良くするためのレポートを作ります。サーバーはなく、ほかの人のチャンネルのデータは扱いません。

英語の文例（フォームに貼る）：
```
This is a personal desktop application (Windows) used only by me, the owner of the YouTube channel
【channel URL】, to edit and publish my own videos. There is no server and no other users.

How the API is used:
1. After I upload a video in YouTube Studio, the app sets the title, description (with chapters),
   tags, category, thumbnail and captions of that video (videos.list, videos.update,
   thumbnails.set, captions.insert, channels.list, playlistItems.list).
2. Optionally, the app uploads the finished video file (videos.insert). Uploads happen only when
   I press the upload button.
3. After publishing, the app reads the audience retention (YouTube Analytics API,
   reports.query with elapsedVideoTimeRatio / audienceWatchRatio) of my own videos, and maps the
   drop-off points to the transcript of the edit, so that I can improve my next video.
4. The app reads comments on my own videos (commentThreads.list) to summarize them and suggest
   reply drafts. Replies are posted (comments.insert) only after I review and confirm each one.

All requests are made with my own OAuth credentials and only for my own channel.
```

### API の使用量（Quota）
1日の使用量の目安（動画1本を投稿する日）：

| 処理 | API | 回数の目安 |
|---|---|---|
| 動画を探す | channels.list / playlistItems.list / videos.list | 数回 |
| タイトル等の設定 | videos.update | 1〜数回 |
| サムネイル | thumbnails.set | 1回 |
| 字幕（使う場合） | captions.insert | 1回 |
| 動画ごとアップロード（使う場合） | videos.insert | 1回 |
| 公開後の分析 | YouTube Analytics API reports.query | 1本あたり2回 |
| コメント | commentThreads.list / comments.insert | 数回 / 返信の数 |

それぞれのユニット数は変わることがあるので、[Quota Calculator](https://developers.google.com/youtube/v3/determine_quota_cost) で最新の値を確認して書いてください。割り当ての追加を頼む場合は、「週に【N】本の動画を投稿し、1本あたり【N】ユニット使う」のように理由を書きます。

英語の文例：
```
I publish about 【N】 videos per week. Setting metadata, thumbnail and captions uses about
【N】 units per video, and an upload uses about 【N】 units. Reading analytics and comments for
recent videos uses a small number of additional units per day.
```

### データの扱い（Data handling / Developer Policies）
このツールの実際の動きです（申請の内容と食い違わないように、ツールを改造したら書き直してください）。

- 認証情報（トークン）は自分の PC の `%APPDATA%\ai-roughcut\youtube_token.json` にだけ保存します
- YouTube から取ったデータ（視聴維持率・コメント）は、自分の PC の作業フォルダ（`○○_roughcut\analytics`）にだけ保存します
- **ほかの人のコメントのデータは、取得してから30日たつと自動で消します**（ツールの「分析」タブを開いたときと、`analytics.py` を実行したときに確認します）。`python analytics.py --purge` で、いつでも全部消せます
- 分析とコメントの要約のために、データの一部（自分の動画の文字起こし・視聴維持率・コメントの文）を **Anthropic の Claude API** に送ります。これはプライバシーポリシーに書きます。Claude API を使わない設定（「AIを使わない」「claude.ai に貼り付けて使う」）もあります
- データを売ったり、広告に使ったり、ほかの人と共有したりはしません

英語の文例：
```
- OAuth tokens are stored only on my own PC.
- API data (audience retention of my own videos, comments on my own videos) is stored only on my
  PC. Comment data is automatically deleted 30 days after it was fetched, and all stored API data
  can be deleted at any time with one command.
- To create the analysis and reply drafts, parts of the data (the transcript of my video,
  retention numbers, comment text) are sent to the Anthropic Claude API. This is disclosed in the
  privacy policy. The app can also be used without sending data to Claude.
- Data is never sold, used for advertising, or shared with anyone else.
- Users can revoke access at https://myaccount.google.com/permissions
```

## 4. プライバシーポリシーの文例

自分のサイトなどに載せる文の例です（日本語と英語の両方を載せると安心です）。

```
【アプリ名】のプライバシーポリシー

【アプリ名】は、【名前】が自分の YouTube チャンネルの動画を編集・投稿・分析するために使う、
パソコン用のツールです。

1. 使う情報
   YouTube API Services を使って、自分のチャンネルの動画の情報・視聴データ（視聴維持率など）・
   動画についたコメントを読み、動画のタイトル・概要欄・サムネイル・字幕の設定、コメントへの返信を行います。
2. 保存
   情報は、このツールを使うパソコンの中にだけ保存します。コメントのデータは30日たつと自動で消します。
3. ほかのサービスへの送信
   分析とコメントの要約のために、情報の一部を Anthropic 社の Claude API に送ります
   （https://www.anthropic.com/legal/privacy）。
4. 共有
   情報を売ったり、ほかの人と共有したりはしません。
5. 許可の取り消し
   https://myaccount.google.com/permissions から、いつでもこのツールへの許可を取り消せます。
6. YouTube の規約
   このツールは YouTube API Services を使っています。YouTube 利用規約
   （https://www.youtube.com/t/terms）と Google プライバシーポリシー
   （https://policies.google.com/privacy）も適用されます。
7. 問い合わせ
   【メールアドレス】
```

## 5. よくある質問への答え方（Google から聞かれやすいこと）

- **「アプリを見せてください」**：準備した画面録画の URL を送ります。ログイン画面（Google の許可の画面）から、設定を反映するところまでが写っていると伝わりやすいです
- **「ほかのユーザーは使えますか？」**：いいえ。自分のチャンネル専用で、配布もしていない、と答えます
- **「データを何日保存しますか？」**：コメントは30日で自動で消す。視聴データは自分のチャンネルのデータで、手元の分析レポートにだけ使う。いつでも消せる、と答えます
