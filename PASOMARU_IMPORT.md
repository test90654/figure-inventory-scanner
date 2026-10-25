# Nightly Pasomaru Import

通常は毎朝3時（日本時間）に差分走査します。新規JANのないページが5ページ連続したら終了し、新規JANを発見すると連続回数をリセットします。新規がなければ通常5ページで終了します。JANが取得できない商品を含むページは連続回数をリセットします。

サイトの新着順は保証できないため、差分走査は全件取得を保証しません。日曜朝3時は全ページを走査し、奥のページの未登録商品と途中で切れていた商品名を回収・補修します。差分走査で見つからない商品は次の全件走査まで登録が遅れる場合があります。初回（既存JANなし）は自動で全件走査します。

ActionsのRun workflowで `full_scan` を有効にすると随時全件走査できます。通常実行では無効のままにしてください。

```sh
python import_pasomaru.py                       # 通常の差分走査
python import_pasomaru.py --max-known-pages 10  # 終了判定を10ページに変更
python import_pasomaru.py --full-scan            # 全件走査・商品名補修
python -m unittest test_import_pasomaru -v
```

取得失敗は例外で終了し、Actions上も失敗として表示します。途中まで追記されたJANは再実行時に重複追加しません。同時実行による重複を避けるため、このワークフローは直列実行します。
