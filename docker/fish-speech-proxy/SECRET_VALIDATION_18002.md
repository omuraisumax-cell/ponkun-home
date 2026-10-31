# 18002 Secret validation

この手順は `ponkun-fish-proxy-secret-validation-18002` だけを対象とする。8000、8080、18000、18001、8788 番のコンテナやサービスを操作しない。秘密の内容、長さ、ハッシュは表示しない。

元SecretはComposeの`secrets`としてマウントする。Compose Secretはコンテナ内で読み取り専用であり、通常の書込み可能なbind mountや環境変数として渡さない。

## 1. 構成の非表示検証

GUI 管理済みSecretファイルのパスを現在のPowerShellプロセスだけへ設定する。値そのものをコマンド履歴へ入力しない。

```powershell
Set-Location "<repository-root>\docker\fish-speech-proxy"
docker compose -f .\compose.secret-validation.yml config --quiet
```

`config --quiet`が終了コード0であることを確認する。通常の`config`出力はホスト側Secretパスを表示し得るため使用しない。

## 2. 独立ビルド（明示操作）

次の操作はイメージをビルドするが、コンテナを起動しない。

```powershell
docker compose -f .\compose.secret-validation.yml build fish-cloud-proxy-secret-validation
```

## 3. 18002起動（別途明示操作）

```powershell
docker compose -f .\compose.secret-validation.yml up -d --no-build fish-cloud-proxy-secret-validation
```

起動後、外部Fish Cloudへ接続しないローカルヘルス確認だけを実行する。

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:18002/healthz -Method Get -TimeoutSec 5
```

合格条件はHTTP 200、`status=ok`、`credential=configured`である。音声生成POSTは実行しない。

## 4. Linux実権限検証

以下は内容を読まず、UID/GIDとmodeだけを終了コードで検査する。

```powershell
docker exec ponkun-fish-cloud-proxy-secret-validation-18002 sh -c 'test "$(id -u)" = 65532 && test "$(id -g)" = 65532 && test "$(stat -c %a /run/ponkun-secrets)" = 700 && test "$(stat -c %u /run/ponkun-secrets)" = 65532 && test "$(stat -c %g /run/ponkun-secrets)" = 65532 && test "$(stat -c %a /run/ponkun-secrets/fish_audio_api_key)" = 400 && test "$(stat -c %u /run/ponkun-secrets/fish_audio_api_key)" = 65532 && test "$(stat -c %g /run/ponkun-secrets/fish_audio_api_key)" = 65532'
docker exec --user 65533:65533 ponkun-fish-cloud-proxy-secret-validation-18002 sh -c 'test ! -r /run/compose-secrets/fish_audio_api_key && test ! -r /run/ponkun-secrets/fish_audio_api_key'
```

両コマンドが無出力かつ終了コード0なら合格。元Secret自体がDocker Desktop上で広いmodeに見えても、`/run/compose-secrets`の親ディレクトリとtmpfs側コピーによって別UIDから読み取れないことを必須条件とする。

## 5. Linux専用単体テスト

次の`run`はサービス本体とは別の一時コンテナをUID 65532で実行する。ポートは公開せず、終了時にその一時コンテナだけを削除する。

```powershell
docker compose -f .\compose.secret-validation.yml run --rm --no-deps --entrypoint python --volume "${PWD}:/validation:ro" --workdir /validation fish-cloud-proxy-secret-validation -m unittest test_secret_runtime_permissions_linux -v
```

Linux専用2件がskipされず成功することを合格条件とする。

## 6. 停止（明示操作）

```powershell
docker compose -f .\compose.secret-validation.yml stop fish-cloud-proxy-secret-validation
```

この手順はコンテナやイメージを削除しない。再開する場合は同じComposeファイルで`start`し、再度ヘルスと権限を確認する。
