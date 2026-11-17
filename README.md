# Codex with Azure OpenAI and Microsoft Entra ID

This repository lets Codex use Azure OpenAI deployments with Microsoft Entra
authentication instead of API keys. A loopback-only router allows Codex's model
picker to switch between deployments hosted on different Azure resources while
keeping one Codex model provider selected.

All committed examples use placeholders. Real deployment names and Azure
resource endpoints belong only in the ignored `routes.local.json` file.

## Why a local router is required

Codex persists model-picker changes as:

- model deployment name
- reasoning effort

It does not change `model_provider` when a model is selected. Defining one
provider per Azure resource therefore works with startup profiles, but selecting
a model hosted on another resource keeps the old endpoint.

This repository instead configures one provider:

```text
Codex -> http://127.0.0.1:8766/v1 -> Azure resource selected by model
```

The router reads the request's `model` field, selects the configured Azure v1
endpoint, and forwards Codex's short-lived Entra bearer token. Model and
reasoning-effort selection can then use the normal Codex `/model` interface.

## Architecture

1. Codex loads `model-catalog.json`, which lists the permitted deployment names
   and supported reasoning efforts.
2. Codex remains signed in with ChatGPT and sends its account bearer only to
   the loopback router.
3. The router discards that incoming bearer and uses `azure_entra_token.py` to
   obtain an Entra token in memory.
4. The router routes by deployment name and sends only the Entra bearer to
   Azure.
5. `azure_openai_router.py` streams the Azure
   Responses API result back to Codex.

The router:

- binds only to loopback
- requires an `Authorization: Bearer` header
- never forwards the incoming ChatGPT bearer to Azure
- caches the Entra bearer only in memory until shortly before expiry
- rejects models missing from the local route map
- preserves streaming Responses API traffic

## Repository layout

| Path | Purpose |
|---|---|
| `azure_entra_token.py` | Refreshable Microsoft Entra token helper |
| `azure_openai_router.py` | Loopback Responses API router |
| `refresh_model_catalog.ps1` | Builds Codex metadata for selected deployments |
| `config/router-provider.toml.example` | Placeholder Codex provider configuration |
| `config/routes.example.json` | Placeholder model-to-resource route map |
| `config/secondary-model.config.toml.example` | Optional placeholder profile |
| `tests/` | Token-helper and HTTP-router tests |

These local files are intentionally ignored:

| Path | Reason |
|---|---|
| `routes.local.json` | Contains real deployment names and Azure endpoints |
| `model-catalog.json` | Contains real deployment names and generated metadata |
| `.venv/` | Local Python environment |

## Prerequisites

- Codex CLI with command-backed model-provider authentication.
- Python available on `PATH`.
- Azure CLI signed in to the correct tenant.
- A data-plane role such as `Cognitive Services OpenAI User` on each target
  Azure OpenAI resource.

Check the active Azure CLI account without requesting or printing a token:

```powershell
az account show --query "{subscription:name, tenant:tenantId, user:user.name}"
```

## Setup

Create the Python environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
```

Create the private route map:

```powershell
Copy-Item .\config\routes.example.json .\routes.local.json
```

Edit `routes.local.json` and replace every placeholder:

```json
{
  "listen_host": "127.0.0.1",
  "listen_port": 8766,
  "routes": {
    "MODEL_DEPLOYMENT_PRIMARY": "https://AZURE_RESOURCE_PRIMARY.openai.azure.com/openai/v1",
    "MODEL_DEPLOYMENT_SECONDARY": "https://AZURE_RESOURCE_SECONDARY.openai.azure.com/openai/v1"
  }
}
```

Validate the private route map:

```powershell
.\.venv\Scripts\python.exe .\azure_openai_router.py `
  --config .\routes.local.json `
  --check
```

Generate the private Codex model catalog:

```powershell
& .\refresh_model_catalog.ps1 -Models `
  "MODEL_DEPLOYMENT_PRIMARY", `
  "MODEL_DEPLOYMENT_SECONDARY"
```

Merge `config\router-provider.toml.example` into
`%USERPROFILE%\.codex\config.toml`. Replace:

- `MODEL_DEPLOYMENT_PRIMARY`
- `C:\path\to\codex-aoai`

Do not overwrite unrelated Codex plugin, sandbox, project-trust, or TUI
settings.

## Running the router

Run these commands from the repository root.

### Foreground

Use foreground mode when you want to see startup and error messages. Keep this
terminal open while using Codex:

```powershell
.\.venv\Scripts\python.exe .\azure_openai_router.py `
  --config .\routes.local.json
```

### Hidden background

Use background mode before opening Codex Desktop:

```powershell
$repo = (Resolve-Path .).Path
$pythonw = Join-Path $repo ".venv\Scripts\pythonw.exe"
$router = Join-Path $repo "azure_openai_router.py"
$config = Join-Path $repo "routes.local.json"

Start-Process `
  -FilePath $pythonw `
  -ArgumentList @("`"$router`"", "--config", "`"$config`"") `
  -WorkingDirectory $repo `
  -WindowStyle Hidden
```

The background router continues serving both Codex CLI and Desktop until the
process stops or Windows restarts. You do not need to open a Codex CLI session
before opening Desktop.

### Check router health

The health endpoint contains no deployment details:

```powershell
Invoke-RestMethod http://127.0.0.1:8766/health
```

Expected values:

```text
status      ok
route_count 2
```

If the health request succeeds, the router is already running. Do not start a
second copy.

## Using Codex

Start Codex normally:

```powershell
codex
```

Use `/model` to select a catalog model and reasoning effort. The provider
remains `azure-router`, while the router changes the Azure endpoint.

After changing `config.toml` or regenerating the model catalog, restart the
long-running Codex app server:

```powershell
codex app-server daemon restart
```

## Context window

Codex 0.169 takes a model's context window only from its catalog entry.
`refresh_model_catalog.ps1` copies Codex's own entry for each model
(`codex debug models`), so the window is OpenAI's default: 272,000 tokens for
the models here, of which Codex counts 95% (258,400) as usable and compacts at
90% (244,800). For longer sessions raise `context_window` and
`max_context_window` in the catalog, then restart Codex.

The ceiling is the deployment's tokens-per-minute limit, not only the model's.
A request larger than that limit is refused before the context check: 1.6M
tokens sent to a GlobalStandard deployment with 1,000K TPM came back 429
"exceeded token rate limit", unbilled. Codex compacts at 90% of the window, so
with 1,000K TPM a window of 960,000 compacts at 864,000 and leaves 136,000 for
the turn's new input and the reply. A larger window needs more TPM, which the
subscription's GlobalStandard quota for the model caps.

A top-level `model_catalog_json` is the catalog for every Codex provider. If
Codex also uses another provider, list its models in the same file.

### Next to copilot-api

copilot-api can serve this router's models next to its own providers: add the
router as an `openai-responses` provider and map the bare deployment names to
it.

```json
"providers": {
  "azure": {
    "type": "openai-responses",
    "baseUrl": "http://127.0.0.1:8766",
    "apiKey": "not-needed",
    "models": {
      "MODEL_DEPLOYMENT_PRIMARY": { "contextWindow": 960000, "maxOutputTokens": 128000 }
    }
  }
},
"modelMappings": { "MODEL_DEPLOYMENT_PRIMARY": "azure/MODEL_DEPLOYMENT_PRIMARY" }
```

The Codex app saves the last-picked model as `model` in `config.toml` without
its provider, so with one gateway provider every model you pick stays valid.
Threads that started on `azure-router` keep calling the router, and the picker
offers them the gateway's models too. For those, add a `passthrough` to
`routes.local.json`: a model whose name starts with the prefix goes to that
loopback endpoint unchanged, with a placeholder bearer instead of the Entra
token or the client's own.

```json
"passthrough": { "m365/": "http://127.0.0.1:4141/v1" }
```

## Authentication modes

Codex uses its normal ChatGPT login because the provider sets
`requires_openai_auth = true`. The router uses Azure CLI's installed
`pythonw.exe` entry point for Entra. It reuses the existing `az login` cache
while bypassing both `az.cmd` and console `python.exe`.

Token refresh does not flash a terminal window. The traced process tree
contains only `pythonw.exe` processes; there are no `cmd.exe`, `python.exe`, or
`conhost.exe` descendants.

Use the full credential chain for a service principal, workload identity,
managed identity, or another supported source:

```powershell
$env:AZURE_OPENAI_CREDENTIAL_MODE = "default"
```

Override the token audience only when required by the Azure resource:

```powershell
$env:AZURE_OPENAI_TOKEN_SCOPE = "https://ai.azure.com/.default"
```

The default audience is:

```text
https://cognitiveservices.azure.com/.default
```

## Adding a model

1. Add the deployment name and its Azure v1 base URL to `routes.local.json`.
2. Regenerate `model-catalog.json` with all permitted deployment names.
3. Restart the router if it is running.
4. Restart the Codex app-server daemon.
5. Open `/model` and select the new deployment and reasoning effort.

No new Codex provider is required when the deployment uses the same local
router.

## Verification

Run the full unit suite:

```powershell
.\.venv\Scripts\python.exe -W error::ResourceWarning `
  -m unittest discover -s .\tests -v
```

Check Codex configuration:

```powershell
codex --strict-config doctor --summary
```

Run a small Responses API request:

```powershell
codex exec --ephemeral "Reply with exactly OK."
```

## Troubleshooting

**The model appears but calls the wrong Azure resource**

Confirm that `model_provider` is `azure-router`, not an endpoint-specific
provider.

**Models or reasoning efforts do not appear**

Regenerate `model-catalog.json`, restart the app-server daemon, and start a new
Codex thread.

**The router reports `unknown_model`**

The request's deployment name is missing from `routes.local.json`.

**The router returns 401**

Codex did not send its ChatGPT bearer token to the loopback provider. Confirm
that you are signed in with ChatGPT and that the provider has
`requires_openai_auth = true`.

**Azure returns 403**

The signed-in identity lacks a data-plane role on the selected Azure OpenAI
resource.

**Azure returns deployment-not-found**

The route points to the wrong resource, or the configured model value is not
the Azure deployment name.

**Codex stops after `Reconnecting .../5`**

Set `stream_max_retries` on the router provider:

```toml
[model_providers.azure-router]
stream_max_retries = 100
```

Codex defaults to `5`; `100` is the hard cap. This can keep a failed request
retrying for a long time. More reconnects do not add Azure quota or guarantee
recovery from a token-rate limit; they only extend Codex's bounded retry window.

## API compatibility

Codex uses the Responses wire API. Azure's OpenAI-compatible v1 surface uses:

```text
https://AZURE_RESOURCE.openai.azure.com/openai/v1/responses
```

The deployment name is sent in the request's `model` field. A dated
`api-version` query parameter is not required for this v1 surface.
