# NomadNet EPUB

Drop EPUBs in a folder; serve them as public NomadNet Micron pages.

## Install

```bash
pip install nomadnet-epub
pip install nomadnet          # required for `serve`
```

From a git checkout:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
pip install nomadnet
```

Reticulum should already be configured (`~/.reticulum`). NomadNet runtime data lives under `data_dir` (default `./data/nomadnetwork`).

## Quick start

```bash
nomadnet-epub init          # writes nomadnet-epub.toml + epubs/
# edit nomadnet-epub.toml
cp book.epub epubs/
nomadnet-epub serve
```

`serve` converts EPUBs, syncs NomadNet config from your TOML, starts `nomadnet --daemon`, watches `epubs/`, and restarts NomadNet when books change.

One-shot convert (no daemon):

```bash
nomadnet-epub convert
```

## Config

Everything lives in **`nomadnet-epub.toml`** (see `nomadnet-epub.toml.example`):

```toml
index_title = "Ben's Library"      # heading on index.mu
description = "Mesh EPUB reader"   # tagline under the heading
node_name = "bens-epub-library"    # NomadNet announce name

epubs = "./epubs"
data_dir = "./data/nomadnetwork"
words = 350
images = "none"   # or "files"
```

| Key | Purpose |
|-----|---------|
| `index_title` | Rendered as the `>` heading on `index.mu` |
| `description` | Text under the heading on `index.mu` |
| `node_name` | NomadNet network announce name (synced into `data_dir/config` on `serve`) |
| `epubs` | Folder to watch for `.epub` files |
| `data_dir` | NomadNet pages/files/state (not your main `~/.nomadnetwork`) |
| `words` | Approx. words per reader page |
| `images` | `none` or `files` |

CLI flags override the file when passed (`--index-title`, `--node-name`, …).

## Notes

- Book indexes use the EPUB table of contents; in-book links resolve to Micron pages.
- EPUB downloads are on each book index, not the root catalog.
- Browse from NomadNet / MeshChat / Sideband. No page auth is configured.
