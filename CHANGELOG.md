# Changelog

## [0.2] - 2026-01-31

### Added
- **Operation Timeout Support**: Add configurable timeout functionality for gNMI operations
  - Global `operation_timeout` parameter in `GNMI connect session` keyword
  - Per-operation `timeout` parameter override for `GNMI get` and `GNMI set` keywords
  - Thread-based timeout enforcement to prevent hung operations

### Changed
- **Packaging Modernization**: Migrate to modern Python packaging standards
  - Replace `setup.py` with `pyproject.toml` (PEP 621)
  - Adopt src layout (`src/GNMI/`)
  - Add `.python-version` file (Python 3.12)
  - Update README with uv installation instructions
  - Remove `requirements.txt` (dependencies now in `pyproject.toml`)
  - Update package metadata (version 0.2, MPL-2.0 license, updated dependencies)

- **Dependencies**: Update minimum versions
  - `robotframework>=7.0` (was >=3.0)
  - `pygnmi>=0.8.15` (was >=0.6.8)
  - `python>=3.8` (was >=3.6)

- **Code Quality**: Add Ruff configuration for linting and formatting

### Fixed
- Thread safety improvements in timeout handling

## [0.1] - Previous Release

### Added
- Initial release with basic gNMI client support
- `GNMI connect session` keyword
- `GNMI get` keyword for retrieving configuration/state
- `GNMI set` keyword for setting configuration
- Support for all pygnmi client options (target, username, password, TLS settings, etc.)
