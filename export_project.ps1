$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot
$DistDir = Join-Path $ProjectRoot "dist"
$StageDir = Join-Path $DistDir "_quantdesk_clean_stage"
$VersionFile = Join-Path $ProjectRoot "quantdesk\version.py"
$VersionText = Get-Content -Raw -Path $VersionFile
$PackageVersion = [regex]::Match($VersionText, 'PACKAGE_VERSION\s*=\s*"([^"]+)"').Groups[1].Value
$AppVersion = [regex]::Match($VersionText, 'APP_VERSION\s*=\s*"([^"]+)"').Groups[1].Value
$ApiSchemaVersion = [regex]::Match($VersionText, 'API_SCHEMA_VERSION\s*=\s*"([^"]+)"').Groups[1].Value
if (-not $PackageVersion -or -not $AppVersion -or -not $ApiSchemaVersion) {
    throw "Unable to parse version constants from quantdesk/version.py"
}
$ReleaseCodename = $AppVersion
if ($AppVersion.StartsWith("$PackageVersion-")) {
    $ReleaseCodename = $AppVersion.Substring($PackageVersion.Length + 1)
}
$VersionedZipName = "quantdesk_clean_v$AppVersion.zip"
$VersionedZipPath = Join-Path $DistDir $VersionedZipName
$ZipPath = Join-Path $DistDir "quantdesk_clean.zip"
$ReleaseArtifactPath = Join-Path $ProjectRoot "RELEASE_ARTIFACT.json"

$KeepRootFiles = @(
    "README.md",
    "CHANGELOG.md",
    "ROADMAP.md",
    "AGENTS.md",
    "architecture_inventory.md",
    "RELEASE_NOTES_v3.5.0.md",
    "RELEASE_NOTES_v3.5.1.md",
    "RELEASE_NOTES_v3.5.1.1.md",
    "RELEASE_NOTES_v3.5.2.md",
    "RELEASE_NOTES_v3.6.0.md",
    "RELEASE_NOTES_v3.6.1.md",
    "RELEASE_NOTES_v3.6.2.md",
    "RELEASE_NOTES_v3.7.0.md",
    "RELEASE_NOTES_v3.7.0.1.md",
    "RELEASE_NOTES_v3.7.0.2.md",
    "RELEASE_NOTES_v3.7.0.3.md",
    "RELEASE_NOTES_v3.7.0.4.md",
    "RELEASE_NOTES_v3.7.0.5.md",
    "RELEASE_NOTES_v3.7.0.6.md",
    "RELEASE_ARTIFACT.json",
    "requirements.txt",
    "requirements-data.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    ".gitignore",
    "QuantDesk.cmd",
    "QuantDesk.vbs",
    "start_ui.cmd",
    "setup_windows.ps1",
    "run_tests.cmd",
    "run_daily_smoke.cmd",
    "self_check.cmd",
    "create_desktop_shortcuts.ps1",
    "create_desktop_shortcuts.cmd",
    "export_project.ps1",
    "export_project.cmd",
    "setup_linux.sh",
    "start_ui.sh",
    "run_tests.sh",
    "self_check.sh",
    "export_project.sh"
)

$KeepRootDirs = @(
    "ai_trader",
    "quantdesk",
    "scripts",
    "app",
    "config",
    "data\calendar",
    "docs",
    "prompts",
    "assets",
    "build",
    "templates",
    "tests"
)

$ExcludedDirNames = @(
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tmp",
    ".vscode",
    ".idea",
    ".git",
    "external_refs",
    ".tmp_tests",
    ".quantdesk_test_tmp",
    "_maintenance",
    "logs",
    "reports",
    "results",
    "state",
    "vectorbt",
    "backtrader",
    "qlib",
    "FinRL",
    "finrl"
)

$ExcludedPathPrefixes = @(
    "data\raw",
    "data\daily",
    "data\news",
    "data\cache",
    "data\universe",
    "data\features",
    "data\paper",
    "data\intraday",
    "data\test_runtime",
    "data\chanlun\runtime",
    "data\chanlun\fractals",
    "data\chanlun\bi",
    "data\chanlun\segments",
    "data\knowledge\sources",
    "data\knowledge\raw_text",
    "data\knowledge\fulltext",
    "build\QuantDesk"
)

$ExcludedFileNames = @("config.local.yaml", "local_data_store_override.yaml", "provider_secrets.json", "llm_config.json", "parameter_sensitivity.json", "paper_portfolio.json", "watchlist.json", "manual_confirmation_queue.json", "live_monitor_events.jsonl", "Thumbs.db")
$ExcludedExtensions = @(".pyc", ".pyd", ".dll", ".so", ".dylib", ".log", ".tmp", ".bak", ".zip", ".exe", ".parquet", ".duckdb", ".sqlite", ".db")

function Get-RelativePath {
    param(
        [Parameter(Mandatory = $true)][string]$Base,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $baseFull = [System.IO.Path]::GetFullPath($Base).TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    $pathFull = [System.IO.Path]::GetFullPath($Path)
    return $pathFull.Substring($baseFull.Length)
}

function Test-IsExcluded {
    param([Parameter(Mandatory = $true)][string]$Path)

    $relative = Get-RelativePath -Base $ProjectRoot -Path $Path
    $parts = $relative -split "[\\/]"
    foreach ($dir in $ExcludedDirNames) {
        if ($parts -contains $dir) {
            return $true
        }
    }
    foreach ($part in $parts) {
        if ($part -like "tmp*") {
            return $true
        }
    }
    foreach ($prefix in $ExcludedPathPrefixes) {
        if ($relative -eq $prefix -or $relative.StartsWith("$prefix\", [System.StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    if ($ExcludedFileNames -contains [System.IO.Path]::GetFileName($Path)) {
        return $true
    }
    if ([System.IO.Path]::GetFileName($Path) -like "*_verify.json") {
        return $true
    }
    if ([System.IO.Path]::GetFileName($Path) -like "*_cli_verify.json") {
        return $true
    }
    if ($ExcludedExtensions -contains [System.IO.Path]::GetExtension($Path)) {
        return $true
    }
    return $false
}

function Copy-ProjectFile {
    param([Parameter(Mandatory = $true)][string]$SourcePath)

    if (Test-IsExcluded -Path $SourcePath) {
        return
    }
    $relative = Get-RelativePath -Base $ProjectRoot -Path $SourcePath
    $destination = Join-Path $StageDir $relative
    $destinationDir = Split-Path -Parent $destination
    if (-not (Test-Path -LiteralPath $destinationDir)) {
        New-Item -ItemType Directory -Path $destinationDir -Force | Out-Null
    }
    Copy-Item -LiteralPath $SourcePath -Destination $destination -Force
}

function Hide-PersonalPathsInStage {
    $changed = 0
    $windowsPattern = 'C:\\Users\\\S+'
    $linuxPattern = '/home/\S+'
    Get-ChildItem -LiteralPath $StageDir -Recurse -File -Force | Where-Object {
        $_.Extension -in @(".py", ".json")
    } | ForEach-Object {
        $text = [System.IO.File]::ReadAllText($_.FullName, [System.Text.Encoding]::UTF8)
        $sanitized = [System.Text.RegularExpressions.Regex]::Replace($text, $windowsPattern, "<USER_HOME>")
        $sanitized = [System.Text.RegularExpressions.Regex]::Replace($sanitized, $linuxPattern, "<USER_HOME>")
        if ($sanitized -ne $text) {
            [System.IO.File]::WriteAllText($_.FullName, $sanitized, [System.Text.Encoding]::UTF8)
            $changed += 1
        }
    }
    if ($changed -gt 0) {
        Write-Host ("Personal paths hidden: {0} files" -f $changed)
    }
}

New-Item -ItemType Directory -Path $DistDir -Force | Out-Null

Get-ChildItem -LiteralPath $DistDir -File -Force -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -like "*.zip" -or $_.Name -like "*.sha256" -or $_.Name -like "*manifest*.json"
} | Remove-Item -Force

$releaseArtifact = [ordered]@{
    package_version = $PackageVersion
    app_version = $AppVersion
    api_schema_version = $ApiSchemaVersion
    release_codename = $ReleaseCodename
    source_root = $ProjectRoot
    artifact_type = "source_clean_zip"
    required_files = @(
        "quantdesk/research/daily_research_snapshot.py",
        "tests/test_v3436_research_dashboard_daily_snapshot.py",
        "quantdesk/research/point_in_time_universe.py",
        "quantdesk/research/factor_research_dashboard.py",
        "quantdesk/backtest_assumptions.py",
        "tests/test_v3438_true_backtest_assumptions_split_and_doc_hygiene.py",
        "quantdesk/paper/multi_strategy_account_book.py",
        "scripts/run_multi_strategy_account_book.py",
        "tests/test_v3440_multi_strategy_account_book.py",
        "quantdesk/intraday/intraday_live_engine.py",
        "quantdesk/intraday/intraday_live_snapshot.py",
        "quantdesk/intraday/intraday_score.py",
        "quantdesk/intraday/intraday_data_policy.py",
        "scripts/run_intraday_live_workbench.py",
        "tests/test_v3441_intraday_live_workbench_and_config_hygiene.py",
        "config/multi_strategy_account_book.example.yaml",
        "quantdesk/research/risk_attribution_lite.py",
        "quantdesk/research/risk_attribution.py",
        "quantdesk/research/factor_attribution.py",
        "quantdesk/research/daily_risk_attribution_snapshot.py",
        "scripts/run_risk_attribution.py",
        "scripts/run_risk_attribution_lite.py",
        "tests/test_v3442_risk_attribution_lite_and_live_polish.py",
        "docs/intraday_live_quickstart.md",
        "quantdesk/research/experiment_tracker.py",
        "quantdesk/research/daily_experiment_tracker_snapshot.py",
        "scripts/run_experiments.py",
        "scripts/clean_release_staging.ps1",
        "quantdesk/research/strategy_recommender.py",
        "quantdesk/research/daily_strategy_recommendation_snapshot.py",
        "quantdesk/strategy_recommender.py",
        "scripts/run_strategy_recommender.py",
        "quantdesk/research/stock_picking_flow.py",
        "quantdesk/research/paper_watchlist.py",
        "quantdesk/research/daily_candidate_review.py",
        "quantdesk/data/price_adjustment.py",
        "quantdesk/data/corporate_actions.py",
        "quantdesk/data/adjusted_price_loader.py",
        "quantdesk/research/dynamic_factor_model.py",
        "quantdesk/research/factors/value.py",
        "quantdesk/research/factors/quality.py",
        "quantdesk/research/factors/microstructure.py",
        "quantdesk/research/factors/sentiment.py",
        "quantdesk/research/factors/event.py",
        "quantdesk/risk/smart_stop.py",
        "quantdesk/risk/risk_alerts.py",
        "quantdesk/risk/portfolio_concentration.py",
        "quantdesk/risk/liquidity_warning.py",
        "quantdesk/notifications/notification_center.py",
        "quantdesk/notifications/channels.py",
        "quantdesk/research/execution_realism.py",
        "quantdesk/research/multiperiod_attribution.py",
        "quantdesk/research/barra_style_attribution.py",
        "quantdesk/research/return_explain.py",
        "quantdesk/research/stress_scenarios.py",
        "quantdesk/research/similar_day.py",
        "quantdesk/research/decision_journal.py",
        "quantdesk/data/money_flow.py",
        "quantdesk/data/dragon_tiger.py",
        "quantdesk/data/fundamental_metrics.py",
        "quantdesk/data/industry_classification.py",
        "quantdesk/data/index_membership_history.py",
        "quantdesk/storage/local_store.py",
        "scripts/run_v360_builder.py",
        "scripts/run_stock_picking_flow.py",
        "scripts/run_paper_watchlist.py",
        "scripts/run_daily_candidate_review.py",
        "scripts/run_data_integrity.py",
        "scripts/run_dynamic_factor_model.py",
        "scripts/run_smart_stop.py",
        "scripts/run_risk_alerts.py",
        "scripts/run_notification_center.py",
        "scripts/run_execution_realism.py",
        "scripts/run_style_attribution.py",
        "scripts/run_return_explain.py",
        "scripts/run_stress_test.py",
        "scripts/run_similar_day.py",
        "scripts/run_decision_journal.py",
        "scripts/run_money_flow.py",
        "scripts/run_dragon_tiger.py",
        "scripts/run_fundamental_metrics.py",
        "scripts/run_local_store.py",
        "tests/test_v3443_experiment_tracker_lite.py",
        "quantdesk/research/chart_renderer.py",
        "scripts/validate_historical_listings.py",
        "scripts/build_historical_listings_template.py",
        "docs/point_in_time_universe.md",
        "docs/examples/historical_listings.example.csv",
        "tests/test_v3450_pit_full_and_static_charts.py",
        "quantdesk/backtest_loaders.py",
        "quantdesk/self_check_render.py",
        "quantdesk/research/__init__.py",
        "quantdesk/research/README.md",
        "quantdesk/intraday/__init__.py",
        "quantdesk/intraday/README.md",
        "quantdesk/paper/__init__.py",
        "quantdesk/paper/README.md",
        "quantdesk/agent/__init__.py",
        "quantdesk/agent/README.md",
        "docs/index.md",
        "docs/quickstart.md",
        "docs/user_guide.md",
        "docs/architecture.md",
        "tests/test_v3451_engineering_docs_and_package_grouping_lite.py",
        "quantdesk/onboarding.py",
        "scripts/first_run_wizard.py",
        "build/QuantDesk.spec",
        "build/quantdesk_installer.nsi",
        "build/build_portable_release.ps1",
        "build/build_installer.ps1",
        "RELEASE_NOTES_v3.5.0.md",
        "RELEASE_NOTES_v3.5.1.md",
        "RELEASE_NOTES_v3.5.1.1.md",
        "RELEASE_NOTES_v3.5.2.md",
        "RELEASE_NOTES_v3.6.0.md",
        "RELEASE_NOTES_v3.6.1.md",
        "RELEASE_NOTES_v3.6.2.md",
        "docs/video_scripts.md",
        "docs/release_build.md",
        "docs/videos/README.md",
        "docs/screenshots/README.md",
        "assets/sample_data_manifest.json",
        "scripts/build_sample_data_package.py",
        "tests/test_v350_battle_ready_portable_release.py",
        "tests/test_v351_true_attribution_release_honesty.py",
        "tests/test_v351_package_migration_truth.py",
        "tests/test_v351_backtest_slimming_regression.py",
        "tests/test_v351_packaging_honesty.py",
        "tests/test_v3511_release_staging_cleanup_and_attribution_stress.py",
        "tests/test_v352_ui_home_and_strategy_recommender.py",
        "tests/test_v360_real_use_stock_picking_flow.py",
        "tests/test_v360_adjusted_price_data_integrity.py",
        "tests/test_v360_dynamic_factor_model_and_factor_library.py",
        "tests/test_v360_smart_stop_and_risk_alerts.py",
        "tests/test_v360_notification_center.py",
        "tests/test_v360_execution_realism.py",
        "tests/test_v360_attribution_stress_and_style.py",
        "tests/test_v360_stress_similar_day_decision_journal.py",
        "tests/test_v360_data_sources_and_local_store.py",
        "tests/test_v360_ui_and_release_hardening.py",
        "quantdesk/risk/position_inputs.py",
        "quantdesk/notifications/dedupe.py",
        "quantdesk/research/daily_risk_alert_snapshot.py",
        "tests/test_v361_real_risk_alerts_and_smart_stop_integration.py",
        "quantdesk/research/daily_research_snapshots.py",
        "tests/test_v362_real_similar_day_stress_and_style_attribution.py",
        "scripts/check_text_encoding_hygiene.py",
        "templates/data/index_daily_sample.csv",
        "templates/data/style_exposures_sample.csv",
        "templates/data/style_factor_returns_sample.csv",
        "docs/data_schemas/similar_day_input.md",
        "docs/data_schemas/stress_scenarios_input.md",
        "docs/data_schemas/style_attribution_input.md",
        "docs/data_schemas/decision_journal.md",
        "quantdesk/data_store/__init__.py",
        "quantdesk/data_store/sqlite_store.py",
        "quantdesk/data_store/schema.py",
        "quantdesk/data_store/repositories.py",
        "quantdesk/data_store/migration.py",
        "quantdesk/data_store/health.py",
        "quantdesk/data_store/dual_writer.py",
        "quantdesk/data_store/query.py",
        "quantdesk/data_store/locks.py",
        "quantdesk/data_store/hash_utils.py",
        "quantdesk/data_store/intraday_parquet.py",
        "quantdesk/data_store/quality_repair.py",
        "quantdesk/data_providers/akshare_provider.py",
        "quantdesk/data_providers/fundamentals_chain.py",
        "quantdesk/data_providers/sector_chain.py",
        "quantdesk/data_providers/intraday_chain.py",
        "scripts/migrate_csv_to_sqlite.py",
        "scripts/run_data_db.py",
        "scripts/run_data_query.py",
        "scripts/run_data_layer_health.py",
        "scripts/run_data_quality_repair.py",
        "tests/test_v370_data_backbone_sqlite_akshare.py",
        "tests/test_v370_csv_to_sqlite_migration_integrity.py",
        "tests/test_v370_data_provider_chain_and_quality.py",
        "tests/test_v3701_strategy_viability_on_data_backbone.py",
        "RELEASE_NOTES_v3.7.0.md",
        "RELEASE_NOTES_v3.7.0.1.md",
        "docs/strategy_viability_sprint.md",
        "quantdesk/research/strategy_viability_data_precheck.py",
        "quantdesk/research/strategy_viability_runner.py",
        "quantdesk/research/strategy_viability_report.py",
        "scripts/run_strategy_viability_precheck.py",
        "scripts/run_strategy_viability_sprint.py",
        "quantdesk/data/schema_normalizer.py",
        "quantdesk/research/strategy_pipeline_repair.py",
        "scripts/run_strategy_pipeline_precheck.py",
        "tests/test_v3702_strategy_pipeline_repair.py",
        "RELEASE_NOTES_v3.7.0.2.md",
        "docs/strategy_pipeline_repair.md",
        "scripts/build_historical_listings.py",
        "scripts/validate_historical_listings.py",
        "scripts/run_data_universe.py",
        "tests/test_v3703_low_price_pipeline_finalization.py",
        "RELEASE_NOTES_v3.7.0.3.md",
        "docs/low_price_strategy_viability.md",
        "quantdesk/maintenance/git_deletion_audit.py",
        "scripts/run_git_deletion_audit.py",
        "scripts/run_maintenance.py",
        "quantdesk/research/capital_lot_sensitivity.py",
        "scripts/run_capital_lot_sensitivity.py",
        "tests/test_v3704_capital_lot_sensitivity_and_git_hygiene.py",
        "RELEASE_NOTES_v3.7.0.4.md",
        "docs/capital_lot_sensitivity.md",
        "docs/git_deletion_audit.md",
        "quantdesk/maintenance/import_alias_reconciliation.py",
        "scripts/run_import_alias_reconciliation.py",
        "tests/test_v3705_git_deletion_reconciliation.py",
        "RELEASE_NOTES_v3.7.0.5.md",
        "docs/import_alias_reconciliation.md",
        "quantdesk/maintenance/strategy_regression_audit.py",
        "scripts/run_strategy_regression_audit.py",
        "tests/test_v3706_strategy_regression_guard_and_report_hygiene.py",
        "RELEASE_NOTES_v3.7.0.6.md",
        "docs/strategy_regression_guard.md",
        "docs/data_schemas/sqlite_data_backbone.md",
        "docs/data_schemas/csv_to_sqlite_migration.md",
        "docs/data_schemas/data_provider_chain.md",
        "docs/data_schemas/intraday_parquet.md"
    )
    generated_at = (Get-Date).ToString("s")
    artifact_note = "This file is embedded in the clean zip to prove the uploaded artifact version."
}
$filteredRequiredFiles = @()
foreach ($relativeFile in $releaseArtifact["required_files"]) {
    $sourcePath = Join-Path $ProjectRoot $relativeFile
    if (Test-Path -LiteralPath $sourcePath -PathType Leaf) {
        $resolvedPath = (Resolve-Path -LiteralPath $sourcePath).ProviderPath
        if (-not (Test-IsExcluded -Path $resolvedPath)) {
            $filteredRequiredFiles += (Get-RelativePath -Base $ProjectRoot -Path $resolvedPath).Replace('\', '/')
        }
    }
}
$releaseArtifact["required_files"] = @($filteredRequiredFiles | Select-Object -Unique)
$releaseArtifactJson = $releaseArtifact | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText(
    $ReleaseArtifactPath,
    $releaseArtifactJson,
    [System.Text.UTF8Encoding]::new($false)
)

if (Test-Path -LiteralPath $StageDir) {
    Remove-Item -LiteralPath $StageDir -Recurse -Force
}
New-Item -ItemType Directory -Path $StageDir -Force | Out-Null

foreach ($file in $KeepRootFiles) {
    $source = Join-Path $ProjectRoot $file
    if (Test-Path -LiteralPath $source -PathType Leaf) {
        Copy-ProjectFile -SourcePath $source
    }
}

foreach ($dir in $KeepRootDirs) {
    $sourceDir = Join-Path $ProjectRoot $dir
    if (Test-Path -LiteralPath $sourceDir -PathType Container) {
        Get-ChildItem -LiteralPath $sourceDir -Recurse -File -Force | ForEach-Object {
            Copy-ProjectFile -SourcePath $_.FullName
        }
    }
}

Hide-PersonalPathsInStage

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open($VersionedZipPath, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    Get-ChildItem -LiteralPath $StageDir -Recurse -File -Force | ForEach-Object {
        $relative = Get-RelativePath -Base $StageDir -Path $_.FullName
        $entryName = $relative.Replace('\', '/')
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $zip,
            $_.FullName,
            $entryName,
            [System.IO.Compression.CompressionLevel]::Optimal
        ) | Out-Null
    }
}
finally {
    $zip.Dispose()
}
Remove-Item -LiteralPath $StageDir -Recurse -Force
Copy-Item -LiteralPath $VersionedZipPath -Destination $ZipPath -Force

function Write-Utf8NoBomJson {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)]$Object,
        [int]$Depth = 8
    )
    $json = $Object | ConvertTo-Json -Depth $Depth
    [System.IO.File]::WriteAllText(
        $Path,
        $json + [Environment]::NewLine,
        [System.Text.UTF8Encoding]::new($false)
    )
}

function Write-CleanZipProof {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][string]$ManifestPath,
        [Parameter(Mandatory=$true)][string]$ShaPath
    )
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
    $entryCount = 0
    $badCount = 0
    $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $entryCount = $archive.Entries.Count
        foreach ($entry in $archive.Entries) {
            $name = $entry.FullName
            if (
                $name -like "dist/*" -or
                $name -like "results/*" -or
                $name -like "reports/*" -or
                $name -like "logs/*" -or
                $name -like "state/*" -or
                $name -like "test_runtime/*" -or
                $name -like "data/daily/*" -or
                $name -like "data/intraday/*" -or
                $name -like "__pycache__/*" -or
                $name -like "*.zip" -or
                $name -like "*.exe"
            ) {
                $badCount += 1
            }
        }
    }
    finally {
        $archive.Dispose()
    }
    $manifest = [ordered]@{
        ok = ($badCount -eq 0)
        status = $(if ($badCount -eq 0) { "passed" } else { "failed" })
        task_type = "clean_zip_manifest"
        artifact_type = "source_clean_zip"
        package_version = $PackageVersion
        app_version = $AppVersion
        api_schema_version = $ApiSchemaVersion
        zip_path = $Path
        sha256 = $hash
        entries = $entryCount
        bad_count = $badCount
        generated_at = (Get-Date).ToString("s")
    }
    Write-Utf8NoBomJson -Path $ManifestPath -Object $manifest
    [System.IO.File]::WriteAllText($ShaPath, $hash + [Environment]::NewLine, [System.Text.Encoding]::ASCII)
}

Write-CleanZipProof `
    -Path $VersionedZipPath `
    -ManifestPath (Join-Path $DistDir "quantdesk_clean_v$AppVersion`_manifest.json") `
    -ShaPath (Join-Path $DistDir "quantdesk_clean_v$AppVersion.sha256")

Write-Host "Clean project exported to: $VersionedZipPath"
Write-Host "Compatibility copy exported to: $ZipPath"
Write-Host "Runtime state and personal data are excluded from the clean zip."
