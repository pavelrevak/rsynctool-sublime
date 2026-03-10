"""
RsyncTool - Sublime Text plugin for rsync synchronization

Push files to remote servers via rsync with support for multiple targets,
exclude patterns and relative paths.
"""
import sublime
import sublime_plugin
import fnmatch
import json
import os
import shlex
import subprocess
import threading


def find_rsyncproject(path):
    """Find .rsyncproject searching upward from path"""
    if not path:
        return None

    directory = path if os.path.isdir(path) else os.path.dirname(path)

    while directory and directory != os.path.dirname(directory):
        candidate = os.path.join(directory, '.rsyncproject')
        if os.path.exists(candidate):
            return candidate
        directory = os.path.dirname(directory)
    return None


def load_rsyncproject(path):
    """Load .rsyncproject, return {} if empty, None on JSON error"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:
                return {}
            # Use ST's decoder (supports trailing commas and comments)
            return sublime.decode_value(content)
    except ValueError as e:
        print(f"RsyncTool: Invalid JSON in {path}: {e}")
        return None
    except IOError:
        return {}


def save_rsyncproject(path, config):
    """Save config to .rsyncproject file"""
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4)
        f.write('\n')


def get_project_root(rsyncproject_path):
    """Return directory containing .rsyncproject"""
    return os.path.dirname(rsyncproject_path)


def get_project_name(rsyncproject_path):
    """Return project name from config or directory name as fallback"""
    config = load_rsyncproject(rsyncproject_path)
    if config:
        return config.get(
            'name') or os.path.basename(os.path.dirname(rsyncproject_path))
    return os.path.basename(os.path.dirname(rsyncproject_path))


def get_active_target(config):
    """Return (target_name, target_value) or (None, None)"""
    targets = config.get('targets', {})
    if not targets:
        return None, None

    active = config.get('active_target')
    if active and active in targets:
        return active, targets[active]

    # Fallback to first target
    name = next(iter(targets))
    return name, targets[name]


def find_all_rsyncprojects(window):
    """Find all .rsyncproject files in window's open folders"""
    projects = []
    for folder in window.folders():
        for root, dirs, files in os.walk(folder):
            if '.rsyncproject' in files:
                projects.append(os.path.join(root, '.rsyncproject'))
            dirs[:] = [d for d in dirs if not d.startswith('.')]
    return projects


def build_project_items(projects, current=None):
    """Build quick panel items for project picker.

    Returns (items, selected_index) where items is list of [label, path].
    """
    items = []
    selected_index = 0
    for i, p in enumerate(projects):
        name = get_project_name(p)
        if p == current:
            label = f"● {name}"
            selected_index = i
        else:
            label = f"  {name}"
        items.append([label, os.path.dirname(p)])
    return items, selected_index


def build_target_items(config):
    """Build quick panel items for target picker.

    Returns (items, target_names, selected_index).
    """
    targets = config.get('targets', {})
    active = config.get('active_target')

    items = []
    target_names = []
    selected_index = 0

    for i, (name, value) in enumerate(targets.items()):
        destination, target_sources, _ = parse_target(value)
        if name == active:
            label = f"● {name}"
            selected_index = i
        else:
            label = f"  {name}"
        if target_sources:
            detail = f"{destination} ({len(target_sources)} sources)"
        else:
            detail = destination
        items.append([label, detail])
        target_names.append(name)

    return items, target_names, selected_index


def parse_target(target_value):
    """Parse target value (string or object).

    Returns (destination, sources, exclude) where sources and exclude
    are None if not overridden in target.
    """
    if isinstance(target_value, str):
        return target_value, None, None

    return (
        target_value.get('destination', ''),
        target_value.get('sources'),
        target_value.get('exclude'),
    )


def is_path_in_sources(file_path, project_root, sources, exclude):
    """Check if file/folder is within sources and not excluded.

    Args:
        file_path: Absolute path to file/folder
        project_root: Absolute path to project root
        sources: List of source paths (may contain .. notation)
        exclude: List of exclude patterns

    Returns:
        True if path should be synced
    """
    if not sources:
        return False

    # Normalize file path
    file_path = os.path.normpath(file_path)

    # Check if file is within any source
    in_sources = False
    for source in sources:
        # Resolve source path relative to project root
        source_abs = os.path.normpath(os.path.join(project_root, source))
        if file_path == source_abs or file_path.startswith(source_abs + os.sep):
            in_sources = True
            break

    if not in_sources:
        return False

    # Check exclude patterns (match like rsync does)
    if exclude:
        filename = os.path.basename(file_path)
        # Get path components for matching (skip .. parts)
        rel_path = os.path.relpath(file_path, project_root)
        path_parts = [p for p in rel_path.replace('\\', '/').split('/') if p and p != '..']

        for pattern in exclude:
            # Match against filename
            if fnmatch.fnmatch(filename, pattern):
                return False
            # Match against each path component (like rsync does)
            for part in path_parts:
                if fnmatch.fnmatch(part, pattern):
                    return False

    return True


def get_rsync_paths(file_path, project_root, sources, destination, is_dir=False):
    """Calculate rsync paths for single file/folder sync.

    Args:
        file_path: Absolute path to file/folder
        project_root: Absolute path to project root (CWD for rsync)
        sources: List of source paths (may contain .. notation)
        destination: Remote destination (e.g., user@host:path/)
        is_dir: True if file_path is a directory

    Returns:
        (local_path, remote_path) for rsync, or (None, None) if not in sources
    """
    file_path = os.path.normpath(file_path)

    for source in sources:
        source_abs = os.path.normpath(os.path.join(project_root, source))
        if file_path == source_abs or file_path.startswith(source_abs + os.sep):
            # Found matching source
            source_basename = os.path.basename(source_abs)
            file_rel_to_source = os.path.relpath(file_path, source_abs)

            if file_rel_to_source == '.':
                # Syncing the source directory itself
                local_path = source.rstrip('/') + '/'
                remote_path = destination.rstrip('/') + '/' + source_basename + '/'
            elif is_dir:
                # Directory within source
                local_path = source.rstrip('/') + '/' + file_rel_to_source + '/'
                remote_path = destination.rstrip('/') + '/' + source_basename + '/' + file_rel_to_source + '/'
            else:
                # File within source
                local_path = source.rstrip('/') + '/' + file_rel_to_source
                file_dir = os.path.dirname(file_rel_to_source)
                if file_dir:
                    remote_path = destination.rstrip('/') + '/' + source_basename + '/' + file_dir + '/'
                else:
                    remote_path = destination.rstrip('/') + '/' + source_basename + '/'

            return local_path.replace('\\', '/'), remote_path

    return None, None


def add_to_config_list(config, field, value, target_name=None):
    """Add value to sources/exclude list (global or per-target).

    Args:
        config: The .rsyncproject config dict
        field: 'sources' or 'exclude'
        value: The path to add
        target_name: None for global, or target name for per-target

    Returns:
        (success, message) tuple
    """
    if target_name is None:
        # Add to global
        items = config.get(field, [])
        if value in items:
            return False, f"'{value}' already in {field}"
        items.append(value)
        config[field] = items
        return True, f"Added '{value}' to {field}"

    # Add to specific target
    targets = config.get('targets', {})
    if target_name not in targets:
        return False, f"Target '{target_name}' not found"

    target = targets[target_name]

    # Convert string target to object
    if isinstance(target, str):
        target = {'destination': target}
        targets[target_name] = target

    # If target doesn't have this field, copy from global first
    if field not in target:
        target[field] = list(config.get(field, []))

    items = target[field]
    if value in items:
        return False, f"'{value}' already in {target_name} {field}"

    items.append(value)
    return True, f"Added '{value}' to {target_name} {field}"


class RsyncContext:
    """Context for current rsync project"""

    _current = {}  # manually selected project per window {window_id: path}

    @classmethod
    def _cleanup(cls):
        """Remove entries for closed windows"""
        valid_ids = {w.id() for w in sublime.windows()}
        for window_id in list(cls._current.keys()):
            if window_id not in valid_ids:
                del cls._current[window_id]

    @classmethod
    def get(cls, view):
        """Get context for view"""
        cls._cleanup()
        window = view.window() if view else sublime.active_window()

        # 1. Manually selected (per window)
        if window:
            current = cls._current.get(window.id())
            if current and os.path.exists(current):
                return current

        # 2. Search from active file
        if view and view.file_name():
            found = find_rsyncproject(view.file_name())
            if found:
                return found

        # 3. Search in open folders
        if window:
            for folder in window.folders():
                found = find_rsyncproject(folder)
                if found:
                    return found

        return None

    @classmethod
    def set(cls, window, path):
        """Manually set project for window"""
        cls._current[window.id()] = path

    @classmethod
    def clear(cls, window):
        """Clear manual selection for window"""
        cls._current.pop(window.id(), None)

    @classmethod
    def is_manual(cls, window):
        """Check if window has manual project selection"""
        return window is not None and window.id() in cls._current


class RsyncProcessManager:
    """Manages running rsync processes per window"""
    _processes = {}  # {window_id: process}
    _status_info = {}  # {window_id: (project_name, target_name)}
    _status_phases = {}  # {window_id: phase}
    _animating = False

    @classmethod
    def set(cls, window_id, process, project_name=None, target_name=None):
        cls.stop(window_id)
        cls._processes[window_id] = process
        cls._status_info[window_id] = (project_name, target_name)
        cls._status_phases[window_id] = 0
        if not cls._animating:
            cls._animating = True
            cls._animate_status()

    @classmethod
    def stop(cls, window_id):
        process = cls._processes.get(window_id)
        if process and process.poll() is None:
            process.terminate()
        cls._processes.pop(window_id, None)
        cls._status_info.pop(window_id, None)
        cls._status_phases.pop(window_id, None)

    @classmethod
    def stop_all(cls):
        for window_id in list(cls._processes.keys()):
            cls.stop(window_id)

    @classmethod
    def is_running(cls, window_id):
        process = cls._processes.get(window_id)
        return process is not None and process.poll() is None

    @classmethod
    def _animate_status(cls):
        """Animate status bar for all running rsync processes"""
        # Clean up finished processes
        for window_id in list(cls._processes.keys()):
            process = cls._processes.get(window_id)
            if process and process.poll() is not None:
                cls._processes.pop(window_id, None)
                cls._status_info.pop(window_id, None)
                cls._status_phases.pop(window_id, None)

        if not cls._processes:
            cls._animating = False
            return

        symbols = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'

        for window_id, process in cls._processes.items():
            if process.poll() is not None:
                continue

            window = None
            for w in sublime.windows():
                if w.id() == window_id:
                    window = w
                    break

            if not window:
                continue

            view = window.active_view()
            if not view:
                continue

            project_name, target_name = cls._status_info.get(
                window_id, ('', ''))
            phase = cls._status_phases.get(window_id, 0)
            symbol = symbols[phase % len(symbols)]
            cls._status_phases[window_id] = phase + 1

            if target_name:
                status = f'RSYNC: {project_name}/{target_name} {symbol}'
            else:
                status = f'RSYNC: {project_name} {symbol}'

            view.set_status('rsync', status)

        sublime.set_timeout(cls._animate_status, 100)


class RsyncToolCommand(sublime_plugin.WindowCommand):
    """Base class for rsync commands"""

    panel_name = 'rsync'
    _panels = {}  # {window_id: panel}

    @classmethod
    def _cleanup_panels(cls):
        """Remove panels for closed windows"""
        valid_ids = {w.id() for w in sublime.windows()}
        for window_id in list(cls._panels.keys()):
            if window_id not in valid_ids:
                del cls._panels[window_id]

    def get_context(self, required=True):
        """Get .rsyncproject and its config"""
        view = self.window.active_view()
        rsyncproject = RsyncContext.get(view)

        if not rsyncproject:
            if required:
                sublime.error_message("No .rsyncproject file found")
            return None, None

        config = load_rsyncproject(rsyncproject)
        if config is None:
            # JSON parse error
            self.get_panel(clear=True)
            self.show_panel()
            self.append_output(f"Error: Invalid JSON in {rsyncproject}\n")
            self.append_output(
                "Check Sublime console for details"
                " (View → Show Console)\n")
            return None, None

        return rsyncproject, config

    def get_panel(self, clear=False):
        """Get or create output panel for this window"""
        self._cleanup_panels()
        window_id = self.window.id()
        if clear or window_id not in RsyncToolCommand._panels:
            RsyncToolCommand._panels[window_id] = self.window.create_output_panel(
                self.panel_name)
        return RsyncToolCommand._panels[window_id]

    def show_panel(self):
        """Show output panel"""
        self.window.run_command(
            'show_panel', {'panel': f'output.{self.panel_name}'})

    def append_output(self, text):
        """Append text to output panel"""
        panel = self.get_panel()
        panel.run_command('append', {'characters': text})
        panel.show(panel.size())

    def run_rsync(self, args, cwd=None, clear=True,
            project_name=None, target_name=None, show_console=True):
        """Run rsync in background thread"""
        settings = sublime.load_settings('rsyncproject.sublime-settings')
        rsync_path = settings.get('rsync_path', 'rsync')

        cmd = [rsync_path] + args
        window_id = self.window.id()

        self.get_panel(clear=clear)
        if show_console:
            self.show_panel()
        self.append_output(f"$ {shlex.join(cmd)}\n")

        thread = threading.Thread(
            target=self._run_process,
            args=(cmd, cwd, window_id, project_name, target_name))
        thread.start()

    def _run_process(self, cmd, cwd, window_id, project_name=None,
            target_name=None):
        """Run process and stream output"""
        try:
            RsyncProcessManager.stop(window_id)

            env = os.environ.copy()

            process = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                text=True)

            RsyncProcessManager.set(window_id, process, project_name, target_name)

            while True:
                line = process.stdout.readline()
                if not line:
                    break
                sublime.set_timeout(
                    lambda l=line: self.append_output(l), 0)

            process.wait()
            returncode = process.returncode

            # Remove from process manager immediately (stops animation)
            RsyncProcessManager.stop(window_id)

            sublime.set_timeout(
                lambda: self.append_output(
                    f"\n[Finished with code {returncode}]\n"), 0)

            if returncode != 0:
                sublime.set_timeout(
                    lambda rc=returncode: self._on_error(rc), 0)
            else:
                sublime.set_timeout(
                    lambda: sublime.status_message("RsyncTool: sync completed"), 0)

            # Restore normal status bar after 3 seconds
            sublime.set_timeout(self._update_status, 3000)

        except FileNotFoundError:
            sublime.set_timeout(
                lambda: self._on_error(None, f"rsync not found: {cmd[0]}"), 0)

    def _on_error(self, returncode, message=None):
        """Handle rsync error - show panel and status message"""
        self.show_panel()
        if message:
            sublime.status_message(f"RsyncTool: {message}")
        else:
            sublime.status_message(f"RsyncTool: sync failed (code {returncode})")

    def _update_status(self):
        """Update status bar"""
        view = self.window.active_view()
        if view:
            view.run_command('rsync_update_status')


class RsyncSyncCommand(RsyncToolCommand):
    """Push files to remote via rsync"""

    def run(self, dry_run=False, pick=False):
        if pick:
            self._dry_run = dry_run
            self._show_project_picker()
            return

        rsyncproject, config = self.get_context()
        if not rsyncproject:
            return

        self._execute_sync(rsyncproject, config, dry_run)

    def _show_project_picker(self):
        """Show project selection, then target selection"""
        self._projects = find_all_rsyncprojects(self.window)
        if not self._projects:
            sublime.error_message("No .rsyncproject found")
            return

        view = self.window.active_view()
        current = RsyncContext.get(view) if view else None
        items, selected_index = build_project_items(self._projects, current)

        self.window.show_quick_panel(
            items, self._on_project_select, selected_index=selected_index)

    def _on_project_select(self, index):
        if index < 0:
            return

        self._rsyncproject = self._projects[index]
        self._config = load_rsyncproject(self._rsyncproject)
        if self._config is None:
            sublime.error_message("Invalid JSON in .rsyncproject")
            return

        self._show_target_picker()

    def _show_target_picker(self):
        """Show target selection"""
        if not self._config.get('targets'):
            sublime.error_message("No targets configured")
            return

        items, self._target_names, selected_index = build_target_items(
            self._config)

        self.window.show_quick_panel(
            items, self._on_target_select, selected_index=selected_index)

    def _on_target_select(self, index):
        if index < 0:
            return

        target_name = self._target_names[index]
        self._config['active_target'] = target_name
        self._execute_sync(self._rsyncproject, self._config, self._dry_run)

    def _execute_sync(self, rsyncproject, config, dry_run):
        """Execute the actual rsync"""
        root = get_project_root(rsyncproject)
        target_name, target_value = get_active_target(config)

        if not target_value:
            sublime.error_message(
                "No targets configured in .rsyncproject")
            return

        # Parse target (string or object with destination/sources/exclude)
        destination, target_sources, target_exclude = parse_target(target_value)

        if not destination:
            sublime.error_message(
                f"Target '{target_name}' has no destination")
            return

        # Use target-specific sources/exclude or fall back to defaults
        sources = target_sources or config.get('sources', [])
        exclude = target_exclude or config.get('exclude', [])

        if not sources:
            sublime.error_message(
                "No sources configured in .rsyncproject")
            return

        # Check if console should be shown
        settings = sublime.load_settings('rsyncproject.sublime-settings')
        self._show_console = settings.get('show_console_during_sync', True)

        # Show project info
        self.get_panel(clear=True)
        if self._show_console:
            self.show_panel()
        self.append_output(f"Project: {get_project_name(rsyncproject)}\n")
        self.append_output(f"Target: {target_name} ({destination})\n")
        if target_sources:
            self.append_output(f"Sources: {', '.join(sources)}\n")
        if dry_run:
            self.append_output("Mode: DRY RUN\n")
        self.append_output("\n")

        # Build rsync command
        flags = config.get('flags', '-rv')
        args = shlex.split(flags)
        if dry_run:
            args.append('-n')

        # --delete flag
        if config.get('delete', False):
            args.append('--delete')

        # Exclude patterns
        for pattern in exclude:
            args.append(f'--exclude={pattern}')

        # Sources
        args.extend(sources)

        # Target (user@host:path/)
        args.append(destination)

        self.run_rsync(
            args, cwd=root, clear=False,
            project_name=get_project_name(rsyncproject),
            target_name=target_name,
            show_console=self._show_console)


class RsyncDryRunCommand(RsyncToolCommand):
    """Dry run - preview rsync without changes"""

    def run(self, pick=False):
        self.window.run_command('rsync_sync', {'dry_run': True, 'pick': pick})


class RsyncStopCommand(sublime_plugin.WindowCommand):
    """Stop running rsync process for this window"""

    def run(self):
        RsyncProcessManager.stop(self.window.id())
        sublime.status_message("RsyncTool: stopped")

    def is_enabled(self):
        return RsyncProcessManager.is_running(self.window.id())


class RsyncToggleConsoleCommand(sublime_plugin.WindowCommand):
    """Toggle show_console_during_sync setting"""

    def run(self):
        settings = sublime.load_settings('rsyncproject.sublime-settings')
        current = settings.get('show_console_during_sync', True)
        settings.set('show_console_during_sync', not current)
        sublime.save_settings('rsyncproject.sublime-settings')
        state = "ON" if not current else "OFF"
        sublime.status_message(f"RsyncTool: console during sync {state}")


class RsyncNewProjectCommand(sublime_plugin.WindowCommand):
    """Create new .rsyncproject file"""

    def run(self, paths=None):
        if paths:
            path = paths[0]
            directory = path if os.path.isdir(path) else os.path.dirname(path)
        else:
            view = self.window.active_view()
            if view and view.file_name():
                directory = os.path.dirname(view.file_name())
            elif self.window.folders():
                directory = self.window.folders()[0]
            else:
                directory = os.path.expanduser("~")

        initial_path = os.path.join(directory, ".rsyncproject")

        self.window.show_input_panel(
            "Create .rsyncproject:",
            initial_path,
            self._on_done,
            None,
            None)

    def _on_done(self, path):
        if not path:
            return

        if not path.endswith(".rsyncproject"):
            path = os.path.join(path, ".rsyncproject")

        if os.path.exists(path):
            if not sublime.ok_cancel_dialog(
                    f"{path}\n\nFile already exists. Overwrite?"):
                return

        directory = os.path.dirname(path)
        if not os.path.exists(directory):
            os.makedirs(directory)

        project_name = os.path.basename(directory)
        config = {
            "name": project_name,
            "targets": {
                "dev": "user@host:path/"
            },
            "active_target": "dev",
            "sources": ["."],
            "exclude": [".*", "*.pyc", "__pycache__"],
            "flags": "-rv",
            "delete": False
        }
        save_rsyncproject(path, config)

        RsyncContext.set(self.window, path)

        view = self.window.active_view()
        if view:
            view.run_command('rsync_update_status')

        sublime.status_message(f"Created {path}")

        # Open the file for editing
        self.window.open_file(path)


class RsyncProjectSettingsCommand(sublime_plugin.WindowCommand):
    """Open .rsyncproject file"""

    def run(self, pick=False):
        if pick:
            self._show_project_picker()
            return

        view = self.window.active_view()
        rsyncproject = RsyncContext.get(view)
        if rsyncproject:
            self.window.open_file(rsyncproject)

    def _show_project_picker(self):
        """Show project selection"""
        self._projects = find_all_rsyncprojects(self.window)
        if not self._projects:
            sublime.error_message("No .rsyncproject found")
            return

        view = self.window.active_view()
        current = RsyncContext.get(view) if view else None
        items, selected_index = build_project_items(self._projects, current)

        self.window.show_quick_panel(
            items, self._on_project_select, selected_index=selected_index)

    def _on_project_select(self, index):
        if index < 0:
            return
        self.window.open_file(self._projects[index])

    def is_enabled(self, pick=False):
        if pick:
            return True
        view = self.window.active_view()
        return RsyncContext.get(view) is not None


class RsyncSelectCommand(sublime_plugin.WindowCommand):
    """Select project and target for keyboard shortcuts"""

    def run(self):
        self._projects = find_all_rsyncprojects(self.window)
        if not self._projects:
            sublime.error_message("No .rsyncproject found")
            return

        view = self.window.active_view()
        current = RsyncContext.get(view) if view else None
        is_manual = RsyncContext.is_manual(self.window)

        # Build items with Auto option first
        items = []
        self._project_list = [None]  # None = auto mode
        selected_index = 0

        auto_label = (
            "● Auto (from current file)" if not is_manual
            else "  Auto (from current file)")
        items.append([auto_label, "Clear manual selection"])

        for i, p in enumerate(self._projects):
            name = get_project_name(p)
            if p == current and is_manual:
                label = f"● {name}"
                selected_index = i + 1
            else:
                label = f"  {name}"
            items.append([label, os.path.dirname(p)])
            self._project_list.append(p)

        self.window.show_quick_panel(
            items, self._on_project_select, selected_index=selected_index)

    def _on_project_select(self, index):
        if index < 0:
            return

        if index == 0:
            # Auto mode
            RsyncContext.clear(self.window)
            self._update_status()
            return

        rsyncproject = self._project_list[index]
        RsyncContext.set(self.window, rsyncproject)

        # Load config and check targets
        config = load_rsyncproject(rsyncproject)
        if config is None:
            self._update_status()
            return

        targets = config.get('targets', {})
        if len(targets) <= 1:
            # Only one or no targets, no need to pick
            self._update_status()
            sublime.status_message(f"Selected: {get_project_name(rsyncproject)}")
            return

        # Multiple targets - show target picker
        self._rsyncproject = rsyncproject
        self._config = config
        self._show_target_picker()

    def _show_target_picker(self):
        """Show target selection"""
        items, self._target_names, selected_index = build_target_items(
            self._config)

        self.window.show_quick_panel(
            items, self._on_target_select, selected_index=selected_index)

    def _on_target_select(self, index):
        if index < 0:
            self._update_status()
            return

        target_name = self._target_names[index]
        self._config['active_target'] = target_name
        save_rsyncproject(self._rsyncproject, self._config)

        self._update_status()
        sublime.status_message(
            f"Selected: {get_project_name(self._rsyncproject)} [{target_name}]")

    def _update_status(self):
        view = self.window.active_view()
        if view:
            view.run_command('rsync_update_status')


class RsyncAddToListCommand(sublime_plugin.WindowCommand):
    """Base class for adding files to sources/exclude lists"""

    field = None  # 'sources' or 'exclude' - override in subclass

    def run(self, paths=None):
        if not paths:
            return

        self._path = paths[0]
        self._rsyncproject = find_rsyncproject(self._path)
        if not self._rsyncproject:
            return

        self._root = get_project_root(self._rsyncproject)
        # Use forward slashes for rsync compatibility (even on Windows)
        self._rel_path = os.path.relpath(self._path, self._root).replace('\\', '/')

        self._config = load_rsyncproject(self._rsyncproject)
        if self._config is None:
            sublime.status_message("Error: Invalid JSON in .rsyncproject")
            return

        # Build quick panel items: [Project (global), Target: xxx, ...]
        self._show_target_picker()

    def _show_target_picker(self):
        """Show quick panel to select global or specific target"""
        targets = self._config.get('targets', {})
        active = self._config.get('active_target')

        items = []
        self._target_names = [None]  # None = global

        # Global option - marked if no target has override for this field
        has_active_override = False
        if active and active in targets:
            _, t_sources, t_exclude = parse_target(targets[active])
            has_active_override = (
                (self.field == 'sources' and t_sources is not None) or
                (self.field == 'exclude' and t_exclude is not None))

        if has_active_override:
            items.append(["  Project (global)", f"Add to global {self.field}"])
        else:
            items.append(["● Project (global)", f"Add to global {self.field}"])

        # Target options
        for name, value in targets.items():
            _, t_sources, t_exclude = parse_target(value)
            has_override = (
                (self.field == 'sources' and t_sources is not None) or
                (self.field == 'exclude' and t_exclude is not None))

            destination, _, _ = parse_target(value)
            if has_override and name == active:
                label = f"● Target: {name}"
            else:
                label = f"  Target: {name}"

            detail = destination
            if has_override:
                detail += f" (has {self.field} override)"

            items.append([label, detail])
            self._target_names.append(name)

        self.window.show_quick_panel(items, self._on_target_select)

    def _on_target_select(self, index):
        if index < 0:
            return

        target_name = self._target_names[index]
        success, message = add_to_config_list(
            self._config, self.field, self._rel_path, target_name)

        if success:
            save_rsyncproject(self._rsyncproject, self._config)

        sublime.status_message(message)

    def is_visible(self, paths=None):
        if not paths:
            return False
        return find_rsyncproject(paths[0]) is not None


class RsyncAddToSourcesCommand(RsyncAddToListCommand):
    """Add file/folder to project sources from sidebar"""
    field = 'sources'


class RsyncAddToExcludeCommand(RsyncAddToListCommand):
    """Add file/folder to project exclude list from sidebar"""
    field = 'exclude'


class RsyncAddToOtherProjectCommand(sublime_plugin.WindowCommand):
    """Add file/folder to another project's sources"""

    def run(self, paths=None):
        if not paths:
            return

        self._path = paths[0]
        self._projects = find_all_rsyncprojects(self.window)

        if not self._projects:
            sublime.error_message("No .rsyncproject found")
            return

        items, _ = build_project_items(self._projects)
        self.window.show_quick_panel(items, self._on_project_select)

    def _on_project_select(self, index):
        if index < 0:
            return

        self._rsyncproject = self._projects[index]
        self._root = get_project_root(self._rsyncproject)
        # Use forward slashes for rsync compatibility (even on Windows)
        self._rel_path = os.path.relpath(self._path, self._root).replace('\\', '/')

        self._config = load_rsyncproject(self._rsyncproject)
        if self._config is None:
            sublime.status_message("Error: Invalid JSON in .rsyncproject")
            return

        # Show target picker (same as RsyncAddToListCommand)
        self._show_target_picker()

    def _show_target_picker(self):
        """Show quick panel to select global or specific target"""
        targets = self._config.get('targets', {})
        active = self._config.get('active_target')

        items = []
        self._target_names = [None]  # None = global

        # Global option - marked if no target has sources override
        has_active_override = False
        if active and active in targets:
            _, t_sources, _ = parse_target(targets[active])
            has_active_override = t_sources is not None

        if has_active_override:
            items.append(["  Project (global)", "Add to global sources"])
        else:
            items.append(["● Project (global)", "Add to global sources"])

        # Target options
        for name, value in targets.items():
            destination, t_sources, _ = parse_target(value)
            has_override = t_sources is not None

            if has_override and name == active:
                label = f"● Target: {name}"
            else:
                label = f"  Target: {name}"

            if has_override:
                detail = f"{destination} (has sources override)"
            else:
                detail = destination

            items.append([label, detail])
            self._target_names.append(name)

        self.window.show_quick_panel(items, self._on_target_select)

    def _on_target_select(self, index):
        if index < 0:
            return

        target_name = self._target_names[index]
        success, message = add_to_config_list(
            self._config, 'sources', self._rel_path, target_name)

        if success:
            save_rsyncproject(self._rsyncproject, self._config)
            project_name = get_project_name(self._rsyncproject)
            if target_name:
                message = f"Added '{self._rel_path}' to {project_name}/{target_name} sources"
            else:
                message = f"Added '{self._rel_path}' to {project_name} sources"

        sublime.status_message(message)

    def is_visible(self, paths=None):
        if not paths:
            return False
        # Check if any folder has a project
        for folder in self.window.folders():
            if find_rsyncproject(folder):
                return True
        return False


class RsyncSyncPathCommand(RsyncToolCommand):
    """Base class for sync file/folder commands"""

    def run(self, paths=None, pick=True):
        if not paths:
            return

        self._path = paths[0]
        self._is_dir = os.path.isdir(self._path)

        if pick:
            self._show_project_picker()
        else:
            view = self.window.active_view()
            rsyncproject = RsyncContext.get(view) if view else None
            if rsyncproject:
                config = load_rsyncproject(rsyncproject)
                if config:
                    self._do_sync(rsyncproject, config)

    def _show_project_picker(self):
        """Show project picker, then target picker"""
        all_projects = find_all_rsyncprojects(self.window)
        if not all_projects:
            sublime.status_message("RsyncTool: no .rsyncproject found")
            return

        # Filter to projects where this path is in sources of any target
        self._projects = []
        for p in all_projects:
            config = load_rsyncproject(p)
            if not config:
                continue
            root = get_project_root(p)
            targets = config.get('targets', {})
            for target_value in targets.values():
                _, target_sources, target_exclude = parse_target(target_value)
                sources = target_sources or config.get('sources', [])
                exclude = target_exclude or config.get('exclude', [])
                if is_path_in_sources(self._path, root, sources, exclude):
                    self._projects.append(p)
                    break

        if not self._projects:
            sublime.status_message("RsyncTool: path not in sources of any project")
            return

        if len(self._projects) == 1:
            self._on_project_select(0)
            return

        items, _ = build_project_items(self._projects)
        self.window.show_quick_panel(items, self._on_project_select)

    def _on_project_select(self, index):
        if index < 0:
            return

        self._rsyncproject = self._projects[index]
        self._config = load_rsyncproject(self._rsyncproject)
        if self._config is None:
            sublime.status_message("RsyncTool: invalid JSON in .rsyncproject")
            return

        self._show_target_picker()

    def _show_target_picker(self):
        """Show target picker"""
        targets = self._config.get('targets', {})
        if not targets:
            sublime.status_message("RsyncTool: no targets configured")
            return

        active = self._config.get('active_target')
        items = []
        self._target_names = []

        for name, value in targets.items():
            destination, target_sources, target_exclude = parse_target(value)
            sources = target_sources or self._config.get('sources', [])
            exclude = target_exclude or self._config.get('exclude', [])
            root = get_project_root(self._rsyncproject)

            if not is_path_in_sources(self._path, root, sources, exclude):
                continue

            label = f"● {name}" if name == active else f"  {name}"
            items.append([label, destination])
            self._target_names.append(name)

        if not items:
            sublime.status_message("RsyncTool: path not in sources for any target")
            return

        if len(items) == 1:
            self._on_target_select(0)
            return

        self.window.show_quick_panel(items, self._on_target_select)

    def _on_target_select(self, index):
        if index < 0:
            return

        target_name = self._target_names[index]
        self._config['active_target'] = target_name
        self._do_sync(self._rsyncproject, self._config)

    def _do_sync(self, rsyncproject, config):
        """Override in subclass to perform actual sync"""
        raise NotImplementedError

    def is_visible(self, paths=None):
        """Show if path is within a rsync project"""
        if not paths:
            return False
        return find_rsyncproject(paths[0]) is not None

    def is_enabled(self, paths=None):
        """Enable only if path is in sources of its project"""
        if not paths:
            return False

        path = paths[0]
        rsyncproject = find_rsyncproject(path)
        if not rsyncproject:
            return False

        config = load_rsyncproject(rsyncproject)
        if not config:
            return False

        root = get_project_root(rsyncproject)
        targets = config.get('targets', {})
        for target_value in targets.values():
            _, target_sources, target_exclude = parse_target(target_value)
            sources = target_sources or config.get('sources', [])
            exclude = target_exclude or config.get('exclude', [])
            if is_path_in_sources(path, root, sources, exclude):
                return True
        return False


class RsyncPushCommand(RsyncSyncPathCommand):
    """Push file/folder to remote"""

    def _do_sync(self, rsyncproject, config):
        """Sync single file or folder to remote"""
        target_name, target_value = get_active_target(config)
        if not target_value:
            return

        destination, target_sources, target_exclude = parse_target(target_value)
        if not destination:
            return

        sources = target_sources or config.get('sources', [])
        exclude = target_exclude or config.get('exclude', [])
        root = get_project_root(rsyncproject)

        # Get rsync paths
        local_path, remote_path = get_rsync_paths(
            self._path, root, sources, destination, self._is_dir)

        if not local_path:
            sublime.status_message("RsyncTool: path not in sources")
            return

        if not is_path_in_sources(self._path, root, sources, exclude):
            sublime.status_message("RsyncTool: path excluded")
            return

        # Check if console should be shown
        settings = sublime.load_settings('rsyncproject.sublime-settings')
        show_console = settings.get('show_console_during_sync', True)

        # Show info in panel
        project_name = get_project_name(rsyncproject)
        self.get_panel(clear=True)
        if show_console:
            self.show_panel()
        self.append_output(f"Project: {project_name}\n")
        self.append_output(f"Target: {target_name} ({destination})\n")
        self.append_output(f"Push: {local_path} -> {remote_path}\n\n")

        # Build rsync args
        flags = config.get('flags', '-rv')
        args = shlex.split(flags)

        for pattern in exclude:
            args.append(f'--exclude={pattern}')

        # Push: local -> remote
        args.append(local_path)
        args.append(remote_path)

        self.run_rsync(
            args, cwd=root, clear=False,
            project_name=project_name, target_name=target_name,
            show_console=show_console)


class RsyncPullCommand(RsyncSyncPathCommand):
    """Pull file/folder from remote"""

    def _do_sync(self, rsyncproject, config):
        """Pull file or folder from remote"""
        target_name, target_value = get_active_target(config)
        if not target_value:
            return

        destination, target_sources, target_exclude = parse_target(target_value)
        if not destination:
            return

        sources = target_sources or config.get('sources', [])
        exclude = target_exclude or config.get('exclude', [])
        root = get_project_root(rsyncproject)

        # Get rsync paths
        local_path, remote_path = get_rsync_paths(
            self._path, root, sources, destination, self._is_dir)

        if not local_path:
            sublime.status_message("RsyncTool: path not in sources")
            return

        if not is_path_in_sources(self._path, root, sources, exclude):
            sublime.status_message("RsyncTool: path excluded")
            return

        # For pull: adjust paths (remote needs full path, local needs parent dir)
        if not self._is_dir:
            remote_file = remote_path.rstrip('/') + '/' + os.path.basename(local_path)
            local_dir = os.path.dirname(local_path)
            if local_dir:
                local_dir += '/'
            else:
                local_dir = './'
            remote_path = remote_file
            local_path = local_dir

        # Check if console should be shown
        settings = sublime.load_settings('rsyncproject.sublime-settings')
        show_console = settings.get('show_console_during_sync', True)

        # Show info in panel
        project_name = get_project_name(rsyncproject)
        self.get_panel(clear=True)
        if show_console:
            self.show_panel()
        self.append_output(f"Project: {project_name}\n")
        self.append_output(f"Target: {target_name} ({destination})\n")
        self.append_output(f"Pull: {remote_path} -> {local_path}\n\n")

        # Build rsync args (reversed: remote -> local)
        flags = config.get('flags', '-rv')
        args = shlex.split(flags)

        for pattern in exclude:
            args.append(f'--exclude={pattern}')

        # Pull: remote -> local
        args.append(remote_path)
        args.append(local_path)

        self.run_rsync(
            args, cwd=root, clear=False,
            project_name=project_name, target_name=target_name,
            show_console=show_console)


class RsyncUpdateStatusCommand(sublime_plugin.TextCommand):
    """Update status bar"""

    def run(self, edit):
        rsyncproject = RsyncContext.get(self.view)

        if rsyncproject:
            config = load_rsyncproject(rsyncproject)
            name = get_project_name(rsyncproject)
            if config:
                target_name, _ = get_active_target(config)
                if target_name:
                    self.view.set_status(
                        'rsync', f'RSYNC: {name}/{target_name}')
                    return
            self.view.set_status('rsync', f'RSYNC: {name}')
        else:
            self.view.erase_status('rsync')


class RsyncEventListener(sublime_plugin.EventListener):
    """Event listener for automatic actions"""

    def on_activated(self, view):
        """Update status bar when switching tabs"""
        view.run_command('rsync_update_status')

    def on_post_save_async(self, view):
        """Auto-sync file on save if enabled"""
        file_path = view.file_name()
        if not file_path:
            return

        # Find project for this file
        rsyncproject = find_rsyncproject(file_path)
        if not rsyncproject:
            return

        config = load_rsyncproject(rsyncproject)
        if not config:
            return

        # Get active target first (needed to check target-level rsync_on_save)
        _target_name, target_value = get_active_target(config)
        if not target_value:
            return

        # Check if rsync_on_save is enabled (target → project → global)
        target_setting = None
        if isinstance(target_value, dict):
            target_setting = target_value.get('rsync_on_save')

        if target_setting is not None:
            enabled = target_setting
        elif config.get('rsync_on_save') is not None:
            enabled = config.get('rsync_on_save')
        else:
            settings = sublime.load_settings('rsyncproject.sublime-settings')
            enabled = settings.get('rsync_on_save', False)

        if not enabled:
            return

        # Parse target
        destination, target_sources, target_exclude = parse_target(target_value)
        if not destination:
            return

        # Get sources and exclude
        sources = target_sources or config.get('sources', [])
        exclude = target_exclude or config.get('exclude', [])

        # Check if file is in sources and not excluded
        root = get_project_root(rsyncproject)
        if not is_path_in_sources(file_path, root, sources, exclude):
            return

        # Get rsync paths
        local_path, remote_path = get_rsync_paths(
            file_path, root, sources, destination, is_dir=False)
        if not local_path:
            return

        # Build rsync command for single file
        settings = sublime.load_settings('rsyncproject.sublime-settings')
        rsync_path = settings.get('rsync_path', 'rsync')

        flags = config.get('flags', '-rv')
        args = shlex.split(flags)

        # Exclude patterns
        for pattern in exclude:
            args.append(f'--exclude={pattern}')

        args.append(local_path)
        args.append(remote_path)

        cmd = [rsync_path] + args

        # Run rsync in background
        def run_sync():
            try:
                result = subprocess.run(
                    cmd, cwd=root, capture_output=True, text=True)
                if result.returncode == 0:
                    sublime.set_timeout(
                        lambda: sublime.status_message(
                            f"RsyncTool: synced {os.path.basename(file_path)}"),
                        0)
                else:
                    sublime.set_timeout(
                        lambda: sublime.status_message(
                            f"RsyncTool: sync failed ({result.returncode})"),
                        0)
            except FileNotFoundError:
                sublime.set_timeout(
                    lambda: sublime.status_message(
                        f"RsyncTool: rsync not found"),
                    0)

        thread = threading.Thread(target=run_sync)
        thread.start()

    def on_exit(self):
        """Stop all rsync processes when Sublime Text exits"""
        RsyncProcessManager.stop_all()
