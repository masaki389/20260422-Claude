# 第10章：Gemini画像生成プロンプト設計編
### 実際に使っているプロンプトをそのまま公開します

---

プロンプトの話は「具体的な例がないと意味がない」と思っているので、この章では実際に使っているプロンプトをそのまま見せながら説明します。

何が機能して何が機能しないか、試行錯誤した結果の話です。

---

## プロンプトの基本構造

まず全体像から。僕が使っているプロンプトはこういう構造になっています。

```
[スタイル指定（英語）]
[キャラクター外見（英語）]
[シーン状況（日本語）]
[カメラ・構図（日本語または英語）]
[除外指定（日本語）]
```

全部日本語でも全部英語でもなくて、目的に応じて使い分けています。

---

## スタイル指定は冒頭に英語で

どんなアニメスタイルで描くかを最初に指定します。

転職チャンネル（情報系・クリーンなアニメ）用：
```
"clean anime illustration style, modern Japanese anime,
vibrant colors, detailed background, professional quality"
```

幸子チャンネル（ドラマ系・温かみのあるアニメ）用：
```
"warm anime illustration style, soft colors, gentle atmosphere,
heartwarming Japanese anime drama style, detailed expressions"
```

スタイル指定をプロンプトの冒頭に置くことで、全体の絵の雰囲気が決まります。これを省くと毎回違うスタイルが出てきやすい。

---

## キャラクター外見は英語で詳細に

第9章でも話しましたが、外見は英語で書きます。

転職チャンネルの主人公（田中悠斗）：
```
"25-year-old Japanese man, short black hair, brown eyes,
clean and neat appearance, fit physique,
wearing white sanitary work uniform,
white hood-type sanitary cap covering entire head,
blue rubber gloves, neat and diligent impression"
```

幸子：
```
"62-year-old Japanese woman, short hair mixed with white and silver,
naturally wavy, gentle warm smile,
slightly round face, soft crow's feet at eye corners,
bright and healthy complexion, typical caring senior Japanese woman,
wearing supermarket staff apron"
```

ポイントは「衣装の詳細まで書くこと」です。手袋の色、キャップの形、エプロンの色。これを省くと毎回違う服装が出てきます。

---

## シーン状況は日本語で具体的に

場所・状況の説明は日本語で書いています。日本的な空間の雰囲気を出すには日本語の方が伝わりやすい感覚があります。

工場の生産ラインシーン（転職系）：
```
「深夜の明るい工場内。白い作業台の前で残業する田中悠斗と同僚2人。
赤・青のプラスチックコンテナが周囲に積まれている。
壁の時計が深夜を示している。蛍光灯の白い照明。」
```

幸子の自宅シーン：
```
「温かみのある自宅の居間。茶色のソファ、白い壁、右側に窓、
正面にテレビ台。夕方の柔らかい日差し。生活感のある小物が置かれている。」
```

シーン説明で「具体的な物の名前と位置」を書くのがポイントです。「きれいな部屋」と書くより「茶色のソファが左側にある部屋」と書く方が、想定した絵に近いものが出ます。

---

## カメラ・構図の指定

カメラの指定は英語で書いた方が効きやすいです。

```
引きショット（場所説明）：
"wide shot, establishing shot, showing the full environment"

バストアップ（語りかけ・感情）：
"bust-up shot, upper body, front-facing camera"

クローズアップ（感情強調）：
"close-up shot, face only, detailed expression"

横顔（運転・考え込む）：
"side profile shot, 3/4 angle from behind"

斜め上から（作業シーン）：
"medium shot from slightly above, diagonal angle"
```

台本のカメラ指定と対応させてプロンプトに入れます。

---

## 除外指定を使う

これ、意外と使っている人が少ない気がするんですが、「描かないでほしいもの」を明示することで品質が上がります。

```
除外してほしいものを書く例：
「企業ロゴ・社名テキスト・文字は一切描かないこと。」
「屋外・雪は描かないこと。完全な室内シーン。」
「正面向きでドアップの顔は描かないこと。」
```

特に「企業ロゴを入れないこと」は必須です。指示しないとAIが勝手に架空のロゴを作って入れてくることがあります。

---

## プロンプトの長さは150〜300文字が適切

長ければ品質が上がるわけではないです。

実験的に400文字以上のプロンプトを試したことがあるんですが、後半の指示が無視されることがよくありました。AIが優先順位を見失う。

逆に50文字以下だと解釈の余地が広すぎて、毎回全然違うものが出てきます。

日本語と英語を合わせて150〜300文字の範囲が、安定している感覚があります。

---

## 実際に使っているプロンプトの全文例

山崎製パン工場の外観カット（カット1）：

```
clean anime illustration style, vibrant colors, modern factory exterior,
nighttime industrial scene

"Deep night, large modern factory building with white metal panel walls.
Red and white delivery trucks lined up at loading dock.
Bright lights illuminating from within the factory.
Wide establishing shot, low angle, showing full building facade."

企業ロゴ・社名テキストは一切入れないこと。深夜の工場の雰囲気。
```

田中悠斗が語りかけるカット（カット2）：

```
clean anime illustration style, vibrant colors

25-year-old Japanese man, short neat black hair, brown eyes,
wearing white sanitary work uniform, white hood-type sanitary cap
(removed, holding in hand), clean appearance

「白い金属パネルの工場建物を背に田中悠斗がカメラ目線で語りかける。
夜の工場前。清潔感のある白衣姿。」

bust-up shot, front-facing, direct eye contact with camera
企業ロゴ・テキストは一切入れないこと。
```

---

## よくある失敗プロンプトの修正例

**問題：顔が崩れる・不自然になる**
```
修正前：「田中悠斗がカメラを見ている」
修正後：bust-up shot, front-facing, direct eye contact, 
        natural expression, detailed face + キャラクター参照画像追加
```

**問題：背景が毎回変わる**
```
修正前：「オフィスで仕事している」
修正後：「参照画像と同じオフィス。白い壁、木製デスク、モニターが正面にある。
         部屋の構造は変えないこと。カメラアングルのみ変更。」
```

**問題：指定した服装が出ない**
```
修正前：「工場の制服を着た田中悠斗」
修正後：「white sanitary work uniform, white hood-type sanitary cap
         covering entire head, blue rubber gloves」+ 制服参照画像追加
```

---

## 【補足】Googleが公式推奨している6レイヤー構造

Googleの公式ガイドとアニメ生成の実践データから、高品質なアニメ画像プロンプトには6つの要素が揃っていることが分かっています。

```
レイヤー1：キャラクター説明（外見・年齢・服装）
レイヤー2：シーン・背景（場所・状況）
レイヤー3：スタイルアンカー（アニメスタイルの指定）
レイヤー4：照明（昼・夜・人工照明・自然光など）
レイヤー5：クオリティマーカー（high quality, detailed, professional）
レイヤー6：除外要素（ロゴなし・特定の要素を入れないこと）
```

実際に使っているプロンプトと照らし合わせると、この6つが全部入っていることが多いです。逆に品質が安定しないときは、どれかが抜けていることが多い。

### 「キャラクターとシーンの分離」が一貫性の鍵

外部のリサーチで見つけた重要な考え方です。

キャラクターの一貫性を保つには「キャラクター説明とスタイルを固定して、アクションと背景だけを変える」という考え方が有効です。

```
【固定する部分（全カット同じ）】
"62-year-old Japanese woman, short white-mixed hair,
warm gentle expression, wearing supermarket apron,
clean anime illustration style"

【カットごとに変える部分】
「スーパーのレジで次のお客さんを迎える場面。
bust-up shot, slight smile, bright fluorescent lighting.」
```

キャラクターの記述を毎回書き直すと、ブレが大きくなります。キャラクター部分は固定テキストとして用意しておいて、毎回コピペする方が安定します。

---

## まとめ

1. **スタイル指定は冒頭に英語で**
   全体の雰囲気を最初に決める

2. **キャラクター外見は英語で詳細に（衣装まで）**
   省くと毎回違う服装になる

3. **シーン状況は日本語で具体的に**
   物の名前と位置を書く

4. **カメラ・構図は英語で**
   bust-up、wide shot、side profileなどを使う

5. **除外指定を使う**
   「描かないでほしいもの」を明示する

6. **長さは150〜300文字**
   長すぎると後半の指示が無視される

7. **6レイヤー構造で抜け漏れをチェック**
   キャラ・シーン・スタイル・照明・品質・除外の6つ

8. **キャラクターの記述は固定してコピペ**
   毎回書き直すとブレる

---

*次の章では、Seedance動画化のプロンプトについて話します。画像を「動かす」ための指示の書き方です。*
