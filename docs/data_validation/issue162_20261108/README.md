# Issue #162 FIFO input preparation

## Purpose and provenance

`scripts/issue162_prepare_fifo_inputs.py` reproduces the two FIFO input CSVs
created by the original one-off PowerShell/Python command. The transformation
was recovered directly from
`%APPDATA%/Microsoft/Windows/PowerShell/PSReadLine/ConsoleHost_history.txt`,
lines 10503-10558 (output setup at 10500-10501; original DB path at 10499).
It does not invoke the 8 UE runs or the 16 FIFO sensitivity runs.

The recovered transformation is unchanged. The wrapper parameterizes paths,
opens SQLite read-only, and requires a new output directory. The original
command read observed/crosswalk CSVs from an exported `repository_files`
folder; the defaults below use the requested local repository copies.

## Inputs

Paths below are relative to the repository root; defaults resolve from the
script location, independently of the working directory.

| Argument | Default input | Verified rows |
| --- | --- | ---: |
| `--observed` | `data/processed/issue162_20261108/observed_chuseok_demand_history.csv` | 4480 |
| `--crosswalk` | `runs/issue162_rebuild/passenger266_rebuild/model_charger_crosswalk.csv` | 266 |
| `--supply-db` | `docs/data_validation/issue162_20261108/passenger266/scenario_sep18.sqlite` | SQLite `charger` table |

Requires Python and pandas (verified with pandas 2.3.3).

## Usage (PowerShell, from repository root)

Choose a writable parent directory. The output directory itself must not exist.
This example creates a unique directory under the repository's `runs` folder:

```powershell
$fifoOut = Join-Path (Get-Location) ("runs/issue162_fifo_inputs_verify_" + [guid]::NewGuid().ToString("N"))
python scripts/issue162_prepare_fifo_inputs.py --out-dir "$fifoOut"
```

Optional `--observed`, `--crosswalk`, and `--supply-db` arguments accept explicit
input paths. Existing output directories are rejected, even when empty.
Existing processed CSVs must not be used as output targets.

To check byte equivalence with the current CSVs:

```powershell
foreach ($name in @("observed_standardized.csv", "me_model_unit_ids.csv")) {
    $generatedHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $fifoOut $name)).Hash
    $currentHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path "data/processed/issue162_20261108" $name)).Hash
    if ($generatedHash -ne $currentHash) { throw "CSV mismatch: $name" }
    Write-Host "$name : exact byte match"
}
```

## Preserved mapping and filtering

1. Read observed and crosswalk identifiers as strings; do not pad or normalize IDs.
2. If present, remove `duplicate_exact` values whose lowercase string is `true` or `1`.
3. Select distinct non-null `(station_id, charger_id, model_charger_id)` mappings;
   reject multiple model IDs for the same observed equipment ID.
4. Left-join observations to mappings and then to supply, validating many-to-one
   joins. Retain matches with a non-null `model_station_id`. Supply model IDs
   and station IDs are converted to strings exactly as in the recovered code.
5. For `observed_standardized.csv`, select the model station ID and numeric
   `reported_charge_min` / `elapsed_min`; drop missing or negative times.
   Preserve the resulting row order. No active-supply filter is added here.
6. For `me_model_unit_ids.csv`, start from the matched observations **before**
   the time-value filter. Select supply rows where `is_active == 1`, deduplicate
   model-ID/unit-count pairs, expand `model_charger_id#1` through `#n_units`,
   then sort the unique IDs. No extra operator or time filter is introduced.
7. Write both CSVs with `index=False` and `encoding="utf-8-sig"`, as originally.

## Verification (2026-11-11)

Generated into a fresh temporary directory, without overwriting current CSVs:

`C:/Users/kimdohyun/Documents/Codex/2026-11-11/referenced-chatgpt-conversation-this-is-an/work/issue162_fifo_verify_lppowder/generated`

| Output | Rows | Columns (in order) | Result |
| --- | ---: | --- | --- |
| `observed_standardized.csv` | 4403 | `station_id,reported_charge_min,elapsed_min` | Exact bytes and parsed content match |
| `me_model_unit_ids.csv` | 92 | `charger_id` | Exact bytes and parsed content match |

SHA-256 (generated and current files):

```text
observed_standardized.csv  0130a838bab48ebeb18f9d51be130d5c433d2f1b5c3bf35264c60bf1e42d76fc
me_model_unit_ids.csv      ec30c91c6b747572aa193c1c87a877e1b2d0a6cbf343c0771546670be023efc3
```

Python compilation passed. Exact DataFrame comparison passed, including column
and row order. Reusing the output directory raised `FileExistsError` as intended.
SHA-256 checks confirmed all three source inputs and both current output CSVs
were unchanged. The Git index was unchanged; no staging or commit was performed.
No UE or FIFO runs were rerun. No output divergence was found.

The initial system-temp attempt encountered a directory access error before
writing either CSV; verification then succeeded in the writable temporary
workspace directory above. The machine-specific temporary directory is for
verification only and is not a required input or a permanent artifact.
