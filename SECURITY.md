# Security

## Credentials

Never commit API keys, broker exports, portfolio files, trade logs, generated
reports, or screenshots containing account information. Runtime artifacts
under `data/` and `reports/` are ignored by Git.

DeepSeek credentials can be supplied through `DEEPSEEK_API_KEY`, a local
`.env.local` file, or:

```text
~/.config/quant_akshare/deepseek_api_key.txt
```

Use file mode `600` for the key file. If a key is ever committed or shared,
revoke it with the provider and create a replacement before publishing.

## Reporting

Please report security issues privately to the repository owner rather than
opening a public issue with credentials, portfolio data, or account details.
