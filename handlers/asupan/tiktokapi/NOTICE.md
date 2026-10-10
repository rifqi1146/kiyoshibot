# Third-party notices — `handlers/asupan/tiktokapi/`

This directory contains vendored code from other open-source projects. The
files are kept close to upstream; do not edit them except when re-vendoring.

## 1. omkarcloud/tiktok-scraper

- Source: https://github.com/omkarcloud/tiktok-scraper
- License: MIT — Copyright (c) 2026 Chetan Jain (see `LICENSE` in this folder)
- Vendored files: `fetch.py`, `parsers.py`, `refs.py`, `search.py`, `videos.py`,
  `shared.py`, `config.py`, `cache_config.py`, `scraper_errors.py`
- Local changes: import paths rewritten to relative (`from . import config`,
  `from .scraper_errors import ...`). The `marshmallow`-based schema modules
  (`schemas.py`, `schema_fields.py`) were intentionally not vendored.

## 2. Evil0ctal/Douyin_TikTok_Download_API

- Source: https://github.com/Evil0ctal/Douyin_TikTok_Download_API
- License: Apache-2.0
- Vendored file: `signer.py` (pure-Python port of TikTok's `webmssdk`
  `X-Dynosaur` / `X-Gnarly`, from `src/dtk/signing/native/tiktok_sign.py`).
  The file keeps its own provenance header at the top.

Both projects are used unmodified except for the import-path adjustments noted
above.
