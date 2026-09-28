# View and Edit — a herdr plugin

[日本語](README.md) | English

> **macOS only. The interface is in Japanese.** This page explains it in English,
> including what the Japanese labels on screen mean.

Open a file whose path is on your [herdr](https://herdr.dev) screen in a popup,
and **view it, edit it, or browse its folder** — without switching to an editor or Finder.

- **On-screen file list** — press the key and every file visible in the pane is listed; pick one to open it.
  Bare names like `REQUIREMENTS.md` and partial paths are looked up inside the project
- **Find by file name** — press `f` and type part of a name to filter every file in the project (like Ctrl+P in VS Code)
- **File browser** — list on the left, preview on the right. Filter, create, rename, move to Trash
- **Viewer** — images and video are shown inside the terminal. ← → switches tables, slides and pages
- **Git change marks and diffs** — changed files get `M`, new files `?`, and so on. Open a file and press `d` to see what changed since the last commit, in color
- **Built-in editor** — Notepad-style keys (Ctrl+S saves, Ctrl+Z undoes).
  Saves in the file's original encoding (such as Shift_JIS) and line endings, and asks before overwriting a file that changed elsewhere

Works with keyboard and mouse. The bottom line always shows the keys you can use right now, and each is clickable.

A step-by-step guide is available in Japanese: [操作ガイド](docs/guide.md).

## Requirements

| What | For | Notes |
|---|---|---|
| macOS | Required | Linux and Windows are not supported |
| [herdr](https://herdr.dev) 0.9.1 or later | Required | |
| Python 3.9 or later | Required | The `python3` that ships with macOS works. If macOS asks to install the Command Line Developer Tools on first use, install them (`xcode-select --install`) |
| A terminal that shows images | Images, video, PDF and similar | A terminal with the Kitty graphics protocol (Ghostty, kitty, WezTerm, …). Tested on Ghostty |
| Google Chrome | Rendering HTML | Falls back to macOS Quick Look (Japanese HTML without a charset declaration may be garbled) |
| ffmpeg | Video playback, seeking audio | `brew install ffmpeg`. Without it you still get the first video frame and audio playback |
| poppler | Paging through PDFs, extracting text | `brew install poppler`. Without it only the first page is shown |

The optional tools are used automatically when installed.

## Install

```bash
herdr plugin install suzuki-junya108/view-and-edit-herdr-plugin
```

herdr lists the commands the plugin will run before installing; review them before you continue.
There is no build step and no extra Python packages.

### Key binding (recommended)

herdr plugins cannot register key bindings themselves, so add this to `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+f"
type = "plugin_action"
command = "view-and-edit.open"
description = "view or edit selected path"
```

Then run `herdr server reload-config` (or `reload config` from the herdr menu).
Change `prefix+f` if it clashes with another binding.

## Usage

| To | Do |
|---|---|
| Open a file shown on screen | `prefix+f` with nothing selected → pick from the list and press `Enter` (or double-click) |
| Open a specific path | Double-click the path to select it, then `prefix+f` |
| Open a file link | **Ctrl+click** a `file://` link (as printed by `eza --hyperlink` and others) |
| Find a file by part of its name | `f` in the list or the file browser → type part of the name → `Enter` |
| Browse the current folder | `..` at the top of the list, or `←`. If no files are on screen, the file browser opens directly |

Paths with a line number such as `src/main.rs:120:5` open at that line.
`a/src/main.rs` (git diff style) and paths wrapped in quotes or brackets work as they are.

**A bare file name is enough.** If the file is not where the text says, the plugin searches the Git repository
that contains the pane's folder for files whose path ends with that name (or with a partial path like `deep/app.py`).
If several match, you choose from a list. Outside Git it searches the pane's folder, shallowest first
(large folders are cut off after about 1 second, and the plugin says so).
Long paths that the terminal or an agent wrapped onto two lines are joined back together.

Once a file is open, `e` edits it and `o` opens it in the default app (Finder and so on).
On every screen `Esc` goes back one screen (and closes on the first one); `?` (`F1` in the editor) lists the keys.

### Keys

**On-screen file list**

| Key | Action |
|---|---|
| ↑↓ / j k | Move (files lower on screen — the newest output — are listed first) |
| Enter / → | Open (at the line number, if any) |
| e | Edit in the built-in editor (at the line number, if any) |
| / | Filter by name |
| f | Find by file name (whole project) |
| ← / Backspace | Browse the current folder |
| q / Esc | Close |

The right side previews the selected file. Click selects, double-click opens.

**File browser**

| Key | Action |
|---|---|
| ↑↓ / j k | Move (PgUp PgDn / g G for page, first, last) |
| Enter / → | Open (enter a folder) |
| ← / Backspace | Parent folder |
| / | Filter by name (Esc clears it) |
| f | Find by file name (the whole project, subfolders included) |
| e / o | Edit in the built-in editor / open in the default app |
| n / m / r | New file / new folder / rename |
| d | Move to Trash (asks first; restore from Finder) |
| c / . / ~ | Copy the path / toggle hidden files / home folder |
| q / Esc | Close |

With the mouse: click selects, double-click opens, `..` at the top goes up, the wheel scrolls.

**Git change marks** (inside Git repositories only; shown in the on-screen list, the file browser and the find screen)

| Mark | Meaning |
|---|---|
| `M` | Modified |
| `A` | Added (a new file already `git add`ed) |
| `R` | Renamed |
| `?` | New, not yet tracked (for a new folder, everything inside it) |
| `U` | Conflicted (during a merge, for example) |
| `•` | Something inside this folder changed |

The heading shows the number of changes in the whole repository (`変更 3 件` = "3 changes"). Marks refresh when you come back from editing.

**Find by file name**

| Key | Action |
|---|---|
| Type | Filter by part of the name (letters in order match: `srcag` finds `src/agent.py`) |
| ↑↓ / PgUp PgDn | Choose |
| Enter | Open (a folder opens in the file browser) |
| Backspace / Ctrl+U | Delete one character / clear |
| Esc | Back to the previous screen |
| F1 | List the keys (`?` is typed as a character here) |

Matches in the file name, consecutive matches and shorter paths rank higher. Up to 200 results are shown; the heading shows the total.

**Viewer**

| Key | Action |
|---|---|
| ↑↓ / PgUp PgDn / g G | Scroll |
| ← → | Switch page, sheet or slide (video and audio: seek 5 seconds) |
| Space | Play / pause video and audio |
| Tab | Switch view (rendered ⇄ source, and so on) |
| d | Changes since the last commit ⇄ normal view. Text files tracked by Git only |
| / n N | Search / next / previous |
| e / o / c / r | Edit / open in the default app / copy the path / reload |
| q / Esc | Back |

With the mouse: click the view names on the second line (`表` table, `プレビュー` preview, …) and `◀` `▶` to switch, click the playback line to play or pause, and scroll with the wheel.

**Editor**

| Key | Action |
|---|---|
| Ctrl+S | Save |
| Esc / Ctrl+Q / Ctrl+W | Close (asks if unsaved; Esc clears the selection first if there is one) |
| Ctrl+Z / Ctrl+Y | Undo / redo |
| Ctrl+C / X / V | Copy / cut / paste (the whole line when nothing is selected) |
| Ctrl+A | Select all |
| Ctrl+F / Ctrl+G | Find / go to line |
| Shift+arrows | Select (or drag with the mouse) |
| Tab / Shift+Tab | Indent / unindent |
| F1 | List the keys (`?` is typed as a character) |

With the mouse: click moves the cursor, drag selects, the wheel scrolls.

**On every screen**, the hints on the bottom line (`Enter 開く` = open, `^S 保存` = save, …) and the buttons in confirmation prompts (`はい` yes, `いいえ` no) are clickable.

## Supported formats

| Kind | Shown as |
|---|---|
| Text and source code | Line numbers and highlighting (Python, JavaScript/TypeScript, Go, Rust, C/C++, Java, shell, Ruby, SQL, CSS, config files, …). UTF-8 / Shift_JIS |
| Markdown | Rendered (headings, lists, checkboxes, tables, code) ⇄ source |
| HTML | Rendered ⇄ text ⇄ source |
| Images | PNG / JPEG / GIF / WebP / HEIC / TIFF / BMP / SVG and more |
| Video | Played in the terminal (with sound) |
| Audio | Play, pause, seek |
| PDF | Pages ⇄ text |
| Word / Excel / PowerPoint | docx rendered and as text, xlsx tables (switch sheets), pptx rendered and text per slide |
| CSV / TSV | Table ⇄ source |
| JSON / JSON Lines / Jupyter Notebook | Pretty-printed |
| SQLite | A table per table (large tables load only the rows on screen) |
| zip / tar | List of contents |
| Anything else | Hex dump |

Only text formats can be edited.

## Privacy and safety

- File contents are never sent over the network.
- When opened with nothing selected, the plugin reads **the text currently visible** in the pane from herdr, only to look for paths. It is not stored or sent anywhere.
- Looking up a bare file name and `f` read the repository's file list with `git ls-files`; outside Git they walk the folder's file names. Neither reads file contents, and the list is not stored (only the file you select is previewed).
- Inside a Git repository the plugin runs `git status` (for the marks) and `git diff-index` (for the diffs) on your machine. They only read; nothing is committed or staged.
  - It never writes Git's index file (`.git/index`), so it does not get in the way of an agent using Git in the same repository.
  - Git can run external commands during comparisons, following the repository's `.git/config`. fsmonitor, filters (clean / process), external diff tools and text conversion are all switched off before Git is called, so they do not run even if planted in the `.git` of a folder you received. The `git ls-files` used for name lookup also runs with fsmonitor off (it does not compare contents, so filters do not run).
- When rendering HTML, **every** attempt by the page to load external images or scripts is blocked (Chrome is started offline).
- SQLite files are opened read-only. Office documents are read without XML DTDs or extremely large parts, so a malicious file cannot exhaust memory.
- Deleting moves to the Trash; nothing is deleted permanently.

## Limitations

- Plain text on screen (a path that is not a link) cannot be opened with a click alone; this is how herdr works. Use the `prefix+f` list, or double-click to select and press the key.
- The list only contains files visible in the pane right now (not what has scrolled away).
- Name lookup searches the Git repository containing the pane's folder (outside Git, the pane's folder).
  `f` searches the whole Git repository containing the folder you are looking at (outside Git, that folder).
  Large folders outside Git are cut off after about 3 seconds, and the plugin says so.
  Files excluded by `.gitignore` do not appear. Files that do not exist yet, or that are on another machine, cannot be opened.
- herdr shows one popup at a time. Opening another path while it is open shows a notice.
- The HTML rendering is the first screenful of the page (the text and source views show the rest).
- Keynote / Numbers / Pages show only the Quick Look preview image.
- Diffs compare the last commit with the current file (staged or not makes no difference).
  A renamed file looks like a completely new file. Deleted files are not listed, so they get no mark.
  Non-text files such as images get no diff view (they do get a mark in the list).

## Update and uninstall

herdr has no update command; installing again gets the latest version.
Use `--ref v0.5.1` (for example) to pin a version.

```bash
herdr plugin install suzuki-junya108/view-and-edit-herdr-plugin      # update
herdr plugin uninstall view-and-edit                                  # uninstall
```

## Development

```bash
herdr plugin link .                          # register the local checkout with herdr
bin/view-and-edit open [path]                # run without herdr
python3 -m unittest discover -s tests -t .   # tests
uvx ruff check . && uvx mypy                 # lint / type check
```

## License

[MIT](LICENSE)
