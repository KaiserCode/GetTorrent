# GetTorrent

**GetTorrent** is a Windows desktop torrent downloader built in Python with Tkinter and libtorrent.

It focuses on a simple download workflow: install it, click a magnet link or open a `.torrent` file, and GetTorrent handles the download.

## Features

- Magnet-link and `.torrent` file support
- Windows single-instance browser handoff
- Automatic magnet and `.torrent` file association
- Real file-payload speed reporting
- Download progress based on wanted file bytes
- `HH:MM:SS` estimated time remaining
- 10 selectable visual themes, including neon/arcade palettes
- Animated theme-gradient progress bars with moving gloss while downloading
- Theme-aware Windows title-bar tint where supported by Windows
- Fully theme-colored in-app File/About menu strip
- Theme-aware text inputs and concurrent-download spinbox
- Persistent torrent-table column widths with Reset Column Sizes
- Proportional torrent-table column resizing with minimum widths
- Persistent torrent queue and download history
- DHT metadata discovery
- Bounded metadata and payload retry diagnostics
- Download-only behavior; completed torrents are detached instead of seeded
- Automatic Python/libtorrent dependency setup
- Microsoft Visual C++ runtime validation/repair
- Windows Start Menu installation

## Installation

1. Download the latest `GetTorrent.zip` from **Releases**.
2. Extract the ZIP.
3. Double-click `BuildAndInstall.cmd`.
4. If Windows opens Default Apps, choose **GetTorrent** for `.torrent` files and `magnet:` links.
5. GetTorrent launches automatically when setup finishes.

After that, clicking a magnet link or opening a `.torrent` file will launch GetTorrent or send the download to the already-running instance.

## Legal use

GetTorrent is a general-purpose peer-to-peer download client. Use it only for files you are legally authorized to download, including your own files, public-domain works, freely licensed content, Linux distributions, and other authorized torrents.

GetTorrent does not provide, index, host, or bundle torrent content.

## Technology

- Python
- Tkinter
- libtorrent
- Windows Registry / shell integration
- PowerShell installation and prerequisite management
- Persistent JSON application state

## License

Copyright © 2026. All rights reserved.

See `LICENSE.txt`. GetTorrent may be downloaded and used in unmodified form. Modification, redistribution, resale, or sublicensing is not authorized except where separately permitted in writing.

A public GitHub repository can still be viewed and forked through GitHub's platform as permitted by GitHub's Terms of Service.

## Support

☕ Buy Me a Coffee: https://buymeacoffee.com/davekaiser

- UI note: themed File/About popup controls and large custom concurrent-download spinner controls are optimized for Windows DPI scaling.
