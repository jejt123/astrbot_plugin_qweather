# Changelog

All notable changes to `astrbot_plugin_qweather` are documented in this file.

## v0.1.6 - 2026-07-13

### Added
- Added `/预警` for manual current weather alert queries with explicit success, no-alert, or failure output.
- Added `/预警检查` for scheduler-style checks that only outputs when new or updated alerts are found.
- Added configurable weather alert response cache duration, defaulting to 10 minutes.
- Added 48-hour weather alert dedupe state retention with automatic cleanup on each warning command run.
- Added current Weather Alert API support using latitude/longitude coordinates and alert attribution formatting.

## v0.1.5 - 2026-07-09

### Changed
- Bumped plugin metadata and AstrBot registration version to `v0.1.5` after completing the recent weather-date switching and logging changes.

## v0.1.4 - 2026-07-09

### Added
- Added configurable QWeather logging level with `debug`, `info`, and `warn` options.
- Added info-level QWeather request/response summaries.
- Added debug-level sanitized request/response payload logging for QWeather API calls.
- Added sensitive-data redaction for API keys, JWT tokens, authorization values, private keys, and secrets in logged URLs and payloads.

## v0.1.3 - 2026-07-09

### Added
- Added JWT authentication support using Ed25519/EdDSA JWTs.
- Added JWT-first authentication behavior with API Key fallback when JWT settings are not configured.
- Added hard validation for partially configured JWT settings to avoid silently falling back to API Key.
- Added JWT token caching and expiry handling.
- Added `PyJWT` and `cryptography` runtime dependencies.

### Added
- Added `/天气` date switching configuration with default `18:00`.
- Before the switch time, `/天气` targets today's detailed weather; at or after the switch time, it targets tomorrow's detailed weather.
- Updated `/天气` fallback formatting and help text to show dynamic `今日天气`/`明日天气` labels.

## v0.1.2 - 2026-07-09

### Added
- Added optional home and work coordinate settings for more precise commute weather lookup.
- Added coordinate normalization and validation for `longitude,latitude` values.
- Updated commute weather lookup to prefer coordinates while preserving address names for display.
- Documented coordinate configuration in README.

## v0.1.1 - 2026-07-09

### Fixed
- Added gzip response handling for QWeather API responses.
- Fixed UTF-8 decode failures when QWeather returns compressed JSON payloads.

### Changed
- Updated plugin repository metadata.

## v0.1.0 - 2026-07-08

### Added
- Added initial AstrBot plugin metadata, README, configuration schema, and requirements file.
- Added `/天气`, `/通勤`, and `/天气帮助` commands.
- Added QWeather client support for location lookup, 3-day daily weather, 24-hour hourly weather, and weather warnings.
- Added rule-based weather, clothing, umbrella, life, and commute fallback advice.
- Added optional model polishing via the configured AstrBot model provider.
- Added commute scene detection based on current time and configured commute windows.
- Added API Key authentication support.
