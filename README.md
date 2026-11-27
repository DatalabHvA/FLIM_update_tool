# FLIM_update_tool

A web tool for updating the content of the FLIM tool. It fetches import figures (UN Comtrade) and
price indices (CBS, Destatis GENESIS-Online, FRED, Eurostat). From them it builds the two input files of the FLIM tool:

| Output | Source | Layout |
|---|---|---|
| `material_market_share.xlsx` | Comtrade (NL imports per partner country) | `Materiaal2`, `partnerISO`, `market_share` (sums to 1 per material) |
| `prijzen.xlsx` | CBS + Destatis + FRED + Eurostat (monthly indices) | Sheet `Datasheet`: row 1 = FLIM ID, row 2 = material, column A = month, values relative to the start month (= 1). Also has the sheets `Ruwe indices` and `Bronnen`. |
| `rapport.xlsx` | — | Status per code, the settings used and the full log |

## How it works

1. **Input**: one Excel file with a sheet per platform: `Comtrade`, `CBS`, `Destatis`, `FRED` and `Eurostat`.
   Each sheet has the columns `Materiaal` and `Code`. The price sheets also have `ID` (the FLIM material ID; every
   material-source combination has its own ID). CBS optionally has `Prijs`, Destatis optionally has `Tabel`, and Eurostat optionally has `Land`. See [voorbeeld_invoer.xlsx](voorbeeld_invoer.xlsx), which you can also download in the tool.
2. **Check**: the tool reads the file and shows errors and warnings: missing columns, codes that look wrong,
   lost leading zeros, duplicate names or IDs.
3. **Settings**: Comtrade year, start and end month for prices, and which platforms to run.
4. **Progress**: a live bar per platform and a status per code (*gelukt / geen data / mislukt*, with
   the reason). A filter shows only the problems, and the log is collapsible.
5. **Result**: download links. If some codes fail, the files still contain everything that did work.
   The banner and `rapport.xlsx` say what is missing.

The fetching itself is done by [comtrade_imports.py](comtrade_imports.py) and [fetch_prices.py](fetch_prices.py).
They still work as before in a notebook or from the command line. The tool only adds the optional
`log=` / `on_progress=` / `on_code=` hooks for progress reporting.

## Destatis codes and tables

The default table is **61241-0004**. It has one variable per GP 2019 code level: 2-, 3-, 4-, 5-, 6- and
selected 9-digit codes. The tool counts the digits after `GP19`, ignoring dashes, and uses the
variable for that level. For example, `GP19-0811` gives 4 digits, `GP19-237011` gives 6 and `GP19-233110002` gives 9.
The variable names are read from the table's metadata on every run. If a code matches no variable,
only that code fails, and its message lists the variables the table has.
A different table per code can be set in the `Tabel` column, for example the older 61241-0006.

## CBS: market prices in the Netherlands (recommended)

For "what buyers in the Netherlands pay, wherever the product was made", CBS **verbruiksprijzen** (consumption prices) are the
best available measure. They combine the import prices paid by Dutch industry and trade with the domestic sales prices of
Dutch producers. They come from table **85770NED**, *Producentenprijzen (PPI); afzet-, invoer-, verbruiksprijzen*, which
starts in 2018. The tool automatically links that table to its predecessor **83935NED** (2012–2023) on their first
12 common months, so the series start in 2015. The **Bronnen** sheet shows which months came from the old table.

- `Code`: the ProdCom/CPA code as CBS writes it, for example `237` (worked natural stone), `2331` (ceramic tiles),
  `2351` (cement), `2361` (concrete products) or `2311` (flat glass).
- `Prijs` (optional): `verbruik` (the default), `invoer`, `binnenland`, `buitenland` or `afzet`.
- No API key is needed.

Destatis (Germany), FRED (US) and Eurostat (EU or another country) measure producer prices elsewhere. They are useful for
comparison or as a backup.

## Eurostat

Eurostat series come from dataset **sts_inppd_m**, producer prices in industry on the domestic market, monthly.
They are unadjusted, with index 2021 = 100. The code is a NACE Rev. 2 code such as `C25` or `C237`.
The country comes from the `Land` column, for example `NL`, `DE` or `EU27_2020`; when it is empty, the default from the
settings is used, which is `EU27_2020`. No API key is needed.
Not every code has figures for every country. For example, `C2311` has no EU27 figures, and the EU27
figures for `C2351` stop after 2023-11. Such a code then shows as *mislukt* with the reason, or the
**Bronnen** sheet shows the period it covers.

## Environment variables

| Variable | Purpose |
|---|---|
| `COMTRADE_API_KEY` | UN Comtrade subscription key |
| `DESTATIS_TOKEN` | GENESIS-Online API token (or `DESTATIS_USER` + `DESTATIS_PASSWORD`) |
| `FRED_API_KEY` | FRED API key |
| `APP_PASSWORD` | **Recommended.** Turns on login (HTTP basic auth) |
| `APP_USERNAME` | Username for that login (default `hva`) |

The start page shows which keys are present. It never shows the values. A missing key makes the codes of
that platform fail with a clear message. The other platforms still run.

## Running locally

```bash
uv venv .venv
```
```bash
uv pip install -p .venv -r requirements-dev.txt
```
```bash
.venv/Scripts/python -m uvicorn app.main:app --reload
```

Then open http://localhost:8000. Tests (with fake APIs, no keys needed):

```bash
.venv/Scripts/python -m pytest -q tests
```

## Deploying on Railway

1. New project → *Deploy from GitHub repo* → this repo. Railway uses the `Dockerfile` and `railway.json`
   (health check on `/healthz`, 1 replica).
2. Under **Variables**, add the keys above, including `APP_PASSWORD`.
3. Under **Settings → Networking**, generate a domain. The URL is public, so the password is what protects the tool.

## Deploying on Azure (later)

The same Docker image runs on **Azure App Service (Linux, container)** or **Azure Container Apps**.

- Secrets: put them in Key Vault and refer to them from App Settings with a
  [Key Vault reference](https://learn.microsoft.com/azure/app-service/app-service-key-vault-references),
  for example `COMTRADE_API_KEY = @Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/comtrade-key/)`.
  The app sees them as normal environment variables, so no code change is needed. Give the app a managed identity
  with *Key Vault Secrets User* rights.
- Access: for HvA accounts only, turn on **Authentication (Easy Auth) with Microsoft Entra ID** and leave
  `APP_PASSWORD` empty. Alternatively, keep `APP_PASSWORD`.
- App Service: set `WEBSITES_PORT=8000`, or set `PORT`.

## Limits

- Runs and results are kept **in memory**. After a restart or redeploy they are gone, so download the files right away.
  Run **one** worker or replica.
- One update runs at a time. A second user gets a "wacht tot die klaar is" message.
- The tool does not change your existing `prijzen.xlsx` with its formulas and extra columns. It builds a clean new file.
  Check once whether the FLIM tool reads the sheet `Datasheet` correctly.
