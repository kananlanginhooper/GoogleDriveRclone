# GoogleDriveRclone

`gdrive-rclone` copies a list of files to Google Drive with [rclone](https://rclone.org) and proves each one arrived: every file is checked against Drive's own MD5 before it counts as copied. It writes one JSON line per file, so whatever runs it (a cron job, a backup catalog, you) knows exactly what's offsite.

It's a single Python file with no dependencies beyond Python 3.9+ and rclone. It knows nothing about where the list came from: no database and no settings file.

## Install

```bash
pip install git+https://github.com/kananlanginhooper/GoogleDriveRclone
```

Or copy `gdrive_rclone.py` anywhere and run it with `python3 gdrive_rclone.py ...`.

## Set up the Google Drive remote

Create an rclone remote of type `drive` (`rclone config`). For a backup, these settings are worth choosing:

- **Your own OAuth client** (client id and secret from Google Cloud Console) instead of rclone's shared one, which is heavily rate-limited.
- **Scope `drive.file`**: rclone can see only the files it created, nothing else in the account.
- **Publish the OAuth app.** An app left in Google's *Testing* mode has its sign-in expire after 7 days, and the backup silently stops. The `docs/` folder here is the homepage and privacy policy this repo's app uses for that.

## Push

```bash
gdrive-rclone push --remote gdrive: --dest backups --list files.jsonl --manifest results.jsonl
```

`files.jsonl` has one JSON object per line:

```json
{"local_path": "/srv/photos/2024/a.jpg", "remote_path": "photos/2024/a.jpg", "sha256": "9f86d08..."}
```

- `remote_path` is relative to `--dest`.
- `sha256` is optional. When given, a file whose content no longer matches it is **not uploaded** (`local_mismatch`), so a damaged local copy never replaces a good offsite one.

For each file, in chunks of at most 300 files or 20 GB (`--max-files`, `--max-gb`):

1. It hashes the local file (SHA-256 and MD5 in one read).
2. It skips the file if Drive already has the same MD5 at that path.
3. It uploads the rest with one `rclone copy`. A file whose local path doesn't end in its `remote_path` goes up on its own with `rclone copyto`.
4. It reads Drive's MD5 back and compares.

Each result is appended to the manifest (default stdout) as soon as it's known:

```json
{"local_path": "...", "remote_path": "backups/photos/2024/a.jpg", "size": 123, "sha256": "...",
 "remote_hash": "...", "hash_type": "md5", "status": "copied", "at": "2026-10-29T20:00:00Z"}
```

| status | meaning |
|---|---|
| `copied` | uploaded, and Drive's MD5 matches |
| `skipped` | already on Drive with the same MD5 |
| `local_mismatch` | the local file no longer matches the `sha256` given; not uploaded |
| `missing` | the local file isn't there |
| `verify_failed` | uploaded, but Drive's MD5 doesn't match (or the file isn't there) |
| `quota_stopped` | not done because Google's daily upload cap was reached |
| `dry_run` | `--dry-run`: planned only |

Other options: `--bwlimit 40M` (passed to rclone) and `--dry-run` (no hashing, no rclone).

### Exit codes

| code | meaning |
|---|---|
| 0 | every file was copied or skipped |
| 7 | Google's daily upload cap (about 750 GB/day) was hit. Run the same list again tomorrow: what's done is skipped. |
| 1 | some files failed (see the manifest) |
| 2 | bad arguments |

## Pull (restore)

```bash
gdrive-rclone pull --remote gdrive: --remote-path backups/photos/2024/a.jpg --to ./restored
```

Copies one file down into `--to` and checks its MD5. It never overwrites: if the file is already in `--to`, it stops.

## Remove (quarantine, never delete)

```bash
gdrive-rclone remove --remote gdrive: --remote-path backups/photos/2024/a.jpg [--quarantine _removed]
gdrive-rclone remove --remote gdrive: --list paths.txt
```

Moves each file to `_removed/<today>/<its path>` on the same Drive. Nothing is deleted. Look through the quarantine folder and empty it yourself when you're sure. With the `drive.file` scope, this can only touch files the tool uploaded.

## Tests

```bash
python tests/test_gdrive_rclone.py -v
```

They run the whole tool against a fake rclone that keeps "Drive" in a temp folder, so no network or real rclone is needed.

## License

Copyright (c) 2026 Kanan Hooper. GPL-3.0. See `LICENSE`.

---
Built by [Kanan Hooper](https://github.com/kananlanginhooper)
