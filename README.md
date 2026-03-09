# RsyncTool - Sublime Text Plugin

Sublime Text 4 plugin for rsync synchronization.

Push files to remote servers via rsync directly from Sublime Text. Configure multiple targets, switch between them, and sync with a single shortcut.

## Features

- **One-click push** - Write code, hit a shortcut, sync to remote
- **Multiple targets** - Switch between dev, staging, production
- **Dry run** - Preview changes before syncing
- **Auto-detection** - Finds `.rsyncproject` from current file upward
- **Manual selection** - Switch between multiple projects
- **Status bar** - Shows active project and target
- **Sidebar integration** - Add files to sources/exclude via right-click

## Quick Start

1. Open Command Palette (`Cmd+Shift+P` / `Ctrl+Shift+P`)
2. Run `Rsync: New Project...` to create `.rsyncproject` in your project root
3. Edit `.rsyncproject` - configure target and sources
4. Use `Rsync: Push to Remote` (`Cmd+Option+R`) to sync

## Installation

### Package Control

1. Install [Package Control](https://packagecontrol.io/installation)
2. Open Command Palette (`Cmd+Shift+P` / `Ctrl+Shift+P`)
3. Run `Package Control: Install Package`
4. Search for `RsyncTool` and install

### Git Clone

```bash
cd ~/Library/Application\ Support/Sublime\ Text/Packages  # macOS
cd ~/.config/sublime-text/Packages  # Linux

git clone https://github.com/pavelrevak/rsynctool-sublime.git
```

### Symlink (Development)

```bash
# macOS
ln -s /path/to/rsynctool-sublime ~/Library/Application\ Support/Sublime\ Text/Packages/rsynctool-sublime

# Linux
ln -s /path/to/rsynctool-sublime ~/.config/sublime-text/Packages/rsynctool-sublime
```

## Commands

All commands available via Command Palette (`Cmd+Shift+P`) with `Rsync:` prefix.

| Command | Description |
|---------|-------------|
| **Push to Remote** | Sync files to active target |
| **Dry Run** | Preview sync without changes |
| **Stop** | Stop running rsync process |
| **New Project...** | Create `.rsyncproject` file |
| **Project Settings** | Open `.rsyncproject` for editing |
| **Select Project...** | Choose active project |
| **Select Target...** | Choose active target |

## Keyboard Shortcuts

| macOS | Windows/Linux | Command |
|-------|---------------|---------|
| `Cmd+Option+R` | `Ctrl+Alt+R` | Push to Remote |
| `Cmd+Option+Shift+R` | `Ctrl+Alt+Shift+R` | Dry Run |

## Sidebar Menu

Right-click on files/folders → **Rsync**:

- **New Project...** - Create new project here
- **Project Settings** - Open project configuration
- **Add to Sources** - Add to sources list
- **Add to Exclude** - Add to exclude list
- **Add to Other Project...** - Add to another project's sources

## Configuration

### .rsyncproject

Create `.rsyncproject` in your project root:

```json
{
    "name": "my-project",
    "targets": {
        "dev": "user@dev-server:project/DEV/",
        "production": "user@prod-server:project/PROD/"
    },
    "active_target": "dev",
    "sources": ["src", "lib", "config"],
    "exclude": [".*", "*.pyc", "__pycache__"],
    "flags": "-rv",
    "delete": false
}
```

| Field | Description |
|-------|-------------|
| `name` | Project name (shown in status bar) |
| `targets` | Map of target names → `user@host:path/` strings |
| `active_target` | Currently selected target |
| `sources` | List of relative paths to sync |
| `exclude` | Glob patterns for `--exclude` |
| `flags` | rsync flags (default: `"-rv"`) |
| `delete` | Add `--delete` flag (default: `false`) |

### Generated rsync command

From the config above:
```
rsync -rv --exclude=".*" --exclude="*.pyc" --exclude="__pycache__" src lib config user@dev-server:project/DEV/
```

With `delete: true`:
```
rsync -rv --delete --exclude=".*" --exclude="*.pyc" --exclude="__pycache__" src lib config user@dev-server:project/DEV/
```

Dry run adds `-n` to flags. CWD is set to project root (directory containing `.rsyncproject`).

### Plugin Settings

`Preferences → Package Settings → RsyncTool → Settings`:

```json
{
    "rsync_path": "rsync"
}
```

## Requirements

- Sublime Text 4 (build 4065+)
- `rsync` (pre-installed on macOS and most Linux distributions)

## License

MIT - Pavel Revak
