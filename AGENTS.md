# AGENTS.md

## Project
klippain-shaketune

## General
- Prefer `rg` for search and `Get-ChildItem` for file discovery.
- Keep changes additive and avoid breaking existing install paths.
- For wheel/CI changes, include a verification step that checks `GLIBC` version requirements and the `EXT_SUFFIX` filename.

## K1C Target
- Python: 3.8.x
- SOABI: cpython-38-mipsel-linux-gnu
- EXT_SUFFIX: .cpython-38-mipsel-linux-gnu.so
- GLIBC: 2.29 (must not exceed this)
