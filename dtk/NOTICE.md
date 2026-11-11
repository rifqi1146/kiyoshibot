# Third-party notice — `dtk/`

This directory contains code from an open-source project. Files are kept close
to upstream; do not edit them except when re-syncing.

## Evil0ctal/Douyin_TikTok_Download_API

- Source: https://github.com/Evil0ctal/Douyin_TikTok_Download_API
- License: Apache-2.0
- Ported subsystems: the platform endpoint tables and query-parameter builders
  (`dtk/platforms/`), the normalized data models (`dtk/models/`), and the native
  request signers — pure-Python `webmssdk` implementations of TikTok's
  `X-Bogus`, `X-Dynosaur` and `X-Gnarly`, plus Douyin's `a_bogus` and Bilibili's
  `wbi` (`dtk/signing/`).
- Local changes: the transport, database, API, worker, CLI and MCP layers of the
  upstream project are present but unused by this bot. Only
  `dtk.signing.native.signer`, `dtk.platforms.tiktok` and their transitive
  imports are exercised, from `handlers/asupan/dtksearch.py`.
