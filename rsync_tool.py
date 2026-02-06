"""
RsyncTool - Sublime Text plugin for rsync synchronization

Push files to remote servers via rsync with support for multiple targets,
exclude patterns and relative paths.
"""
import sublime
import sublime_plugin
import subprocess
import threading
import json
import os
import re
import shlex


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
        with open(path, 'r') as f:
            content = f.read().strip()
            if not content:
                return {}
            # Remove trailing commas (not valid JSON but common)
            content = re.sub(r',\s*([}\]])', r'\1', content)
            return json.loads(content)
    except json.JSONDecodeError as e:
        print(f"RsyncTool: Invalid JSON in {path}: {e}")
        return None
    except IOError:
        return {}


def save_rsyncproject(path, config):
    """Save config to .rsyncproject file"""
    with open(path, 'w') as f:
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


class RsyncContext:
    """Context for current rsync project"""

    _current = None  # manually selected project

    @classmethod
    def get(cls, view):
        """Get context for view"""
        # 1. Manually selected
        if cls._current and os.path.exists(cls._current):
            return cls._current

        # 2. Search from active file
        if view and view.file_name():
            found = find_rsyncproject(view.file_name())
            if found:
                return found

        # 3. Search in open folders
        window = view.window() if view else sublime.active_window()
        if window:
            for folder in window.folders():
                found = find_rsyncproject(folder)
                if found:
                    return found

        return None

    @classmethod
    def set(cls, path):
        """Manually set project"""
        cls._current = path

    @classmethod
    def clear(cls):
        """Clear manual selection"""
        cls._current = None


class RsyncProcessManager:
    """Manages running rsync process"""
    _process = None

    @classmethod
    def set(cls, process):
        cls.stop()
        cls._process = process

    @classmethod
    def stop(cls):
        if cls._process and cls._process.poll() is None:
            cls._process.terminate()
            cls._process = None

    @classmethod
    def is_running(cls):
        return cls._process is not None and cls._process.poll() is None


class RsyncToolCommand(sublime_plugin.WindowCommand):
    """Base class for rsync commands"""

    panel_name = 'rsync'
    _panel = None

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
        """Get or create output panel"""
        if clear or RsyncToolCommand._panel is None:
            RsyncToolCommand._panel = self.window.create_output_panel(
                self.panel_name)
        return RsyncToolCommand._panel

    def show_panel(self):
        """Show output panel"""
        self.window.run_command(
            'show_panel', {'panel': f'output.{self.panel_name}'})

    def append_output(self, text):
        """Append text to output panel"""
        panel = self.get_panel()
        panel.run_command('append', {'characters': text})
        panel.show(panel.size())

    def run_rsync(self, args, cwd=None, clear=True):
        """Run rsync in background thread"""
        settings = sublime.load_settings('rsyncproject.sublime-settings')
        rsync_path = settings.get('rsync_path', 'rsync')

        cmd = [rsync_path] + args

        self.get_panel(clear=clear)
        self.show_panel()
        self.append_output(f"$ {shlex.join(cmd)}\n")

        thread = threading.Thread(
            target=self._run_process,
            args=(cmd, cwd))
        thread.start()

    def _run_process(self, cmd, cwd):
        """Run process and stream output"""
        try:
            RsyncProcessManager.stop()

            env = os.environ.copy()

            process = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                text=True)

            RsyncProcessManager.set(process)

            while True:
                line = process.stdout.readline()
                if not line:
                    break
                sublime.set_timeout(
                    lambda l=line: self.append_output(l), 0)

            process.wait()

            sublime.set_timeout(
                lambda: self.append_output(
                    f"\n[Finished with code {process.returncode}]\n"), 0)

            # Update status bar
            sublime.set_timeout(lambda: self._update_status(), 0)

        except FileNotFoundError:
            sublime.set_timeout(
                lambda: sublime.error_message(
                    f"rsync not found: {cmd[0]}"), 0)

    def _update_status(self):
        """Update status bar"""
        view = self.window.active_view()
        if view:
            view.run_command('rsync_update_status')


class RsyncSyncCommand(RsyncToolCommand):
    """Push files to remote via rsync"""

    def run(self, dry_run=False):
        rsyncproject, config = self.get_context()
        if not rsyncproject:
            return

        root = get_project_root(rsyncproject)
        target_name, target_value = get_active_target(config)

        if not target_value:
            sublime.error_message(
                "No targets configured in .rsyncproject")
            return

        sources = config.get('sources', [])
        if not sources:
            sublime.error_message(
                "No sources configured in .rsyncproject")
            return

        # Show project info
        self.get_panel(clear=True)
        self.show_panel()
        self.append_output(f"Project: {get_project_name(rsyncproject)}\n")
        self.append_output(f"Target: {target_name} ({target_value})\n")
        if dry_run:
            self.append_output("Mode: DRY RUN\n")
        self.append_output("\n")

        # Build rsync command
        flags = config.get('flags', '-rv')
        if dry_run:
            flags = flags + 'n'

        args = [flags]

        # --delete flag
        if config.get('delete', False):
            args.append('--delete')

        # Exclude patterns
        for pattern in config.get('exclude', []):
            args.append(f'--exclude={pattern}')

        # Sources
        args.extend(sources)

        # Target (user@host:path/)
        args.append(target_value)

        self.run_rsync(args, cwd=root, clear=False)


class RsyncDryRunCommand(RsyncToolCommand):
    """Dry run - preview rsync without changes"""

    def run(self):
        self.window.run_command('rsync_sync', {'dry_run': True})


class RsyncStopCommand(sublime_plugin.WindowCommand):
    """Stop running rsync process"""

    def run(self):
        RsyncProcessManager.stop()
        sublime.status_message("RsyncTool: stopped")

    def is_enabled(self):
        return RsyncProcessManager.is_running()


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

        RsyncContext.set(path)

        view = self.window.active_view()
        if view:
            view.run_command('rsync_update_status')

        sublime.status_message(f"Created {path}")

        # Open the file for editing
        self.window.open_file(path)


class RsyncProjectSettingsCommand(sublime_plugin.WindowCommand):
    """Open .rsyncproject file"""

    def run(self):
        view = self.window.active_view()
        rsyncproject = RsyncContext.get(view)
        if rsyncproject:
            self.window.open_file(rsyncproject)

    def is_enabled(self):
        view = self.window.active_view()
        return RsyncContext.get(view) is not None


class RsyncSelectProjectCommand(sublime_plugin.WindowCommand):
    """Manual project selection"""

    def run(self):
        projects = []

        for folder in self.window.folders():
            for root, dirs, files in os.walk(folder):
                if '.rsyncproject' in files:
                    projects.append(
                        os.path.join(root, '.rsyncproject'))
                dirs[:] = [d for d in dirs if not d.startswith('.')]

        if not projects:
            sublime.error_message("No .rsyncproject found")
            return

        view = self.window.active_view()
        current = RsyncContext.get(view) if view else None
        is_manual = RsyncContext._current is not None

        items = []
        self._projects = [None]  # None = auto mode

        auto_label = (
            "● Auto (from current file)" if not is_manual
            else "Auto (from current file)")
        items.append([auto_label, "Clear manual selection"])

        for p in projects:
            project_dir = os.path.dirname(p)
            name = get_project_name(p)
            if p == current and is_manual:
                label = f"● {name}"
            else:
                label = f"  {name}"
            items.append([label, project_dir])
            self._projects.append(p)

        def on_select(index):
            if index < 0:
                return
            if index == 0:
                RsyncContext.clear()
            else:
                RsyncContext.set(self._projects[index])

            view = self.window.active_view()
            if view:
                view.run_command('rsync_update_status')

        self.window.show_quick_panel(items, on_select)


class RsyncSelectTargetCommand(sublime_plugin.WindowCommand):
    """Select active target within current project"""

    def run(self):
        view = self.window.active_view()
        rsyncproject = RsyncContext.get(view)
        if not rsyncproject:
            sublime.error_message("No .rsyncproject file found")
            return

        config = load_rsyncproject(rsyncproject)
        if config is None:
            sublime.error_message("Invalid JSON in .rsyncproject")
            return

        targets = config.get('targets', {})
        if not targets:
            sublime.error_message("No targets configured in .rsyncproject")
            return

        active = config.get('active_target')
        self._rsyncproject = rsyncproject

        items = []
        self._target_names = []
        for name, value in targets.items():
            if name == active:
                label = f"● {name}"
            else:
                label = f"  {name}"
            items.append([label, value])
            self._target_names.append(name)

        def on_select(index):
            if index < 0:
                return
            config['active_target'] = self._target_names[index]
            save_rsyncproject(self._rsyncproject, config)

            view = self.window.active_view()
            if view:
                view.run_command('rsync_update_status')

            sublime.status_message(
                f"Active target: {self._target_names[index]}")

        self.window.show_quick_panel(items, on_select)


class RsyncAddToSourcesCommand(sublime_plugin.WindowCommand):
    """Add file/folder to project sources from sidebar"""

    def run(self, paths=None):
        if not paths:
            return

        path = paths[0]
        rsyncproject = find_rsyncproject(path)
        if not rsyncproject:
            return

        root = get_project_root(rsyncproject)
        rel_path = os.path.relpath(path, root)

        config = load_rsyncproject(rsyncproject)
        if config is None:
            sublime.status_message("Error: Invalid JSON in .rsyncproject")
            return

        sources = config.get('sources', [])

        if rel_path in sources:
            sublime.status_message(f"'{rel_path}' already in sources")
            return

        sources.append(rel_path)
        config['sources'] = sources
        save_rsyncproject(rsyncproject, config)
        sublime.status_message(f"Added '{rel_path}' to sources")

    def is_visible(self, paths=None):
        if not paths:
            return False
        return find_rsyncproject(paths[0]) is not None


class RsyncAddToExcludeCommand(sublime_plugin.WindowCommand):
    """Add file/folder to project exclude list from sidebar"""

    def run(self, paths=None):
        if not paths:
            return

        path = paths[0]
        rsyncproject = find_rsyncproject(path)
        if not rsyncproject:
            return

        root = get_project_root(rsyncproject)
        rel_path = os.path.relpath(path, root)

        config = load_rsyncproject(rsyncproject)
        if config is None:
            sublime.status_message("Error: Invalid JSON in .rsyncproject")
            return

        exclude = config.get('exclude', [])

        if rel_path in exclude:
            sublime.status_message(f"'{rel_path}' already in exclude")
            return

        exclude.append(rel_path)
        config['exclude'] = exclude
        save_rsyncproject(rsyncproject, config)
        sublime.status_message(f"Added '{rel_path}' to exclude")

    def is_visible(self, paths=None):
        if not paths:
            return False
        return find_rsyncproject(paths[0]) is not None


class RsyncAddToOtherProjectCommand(sublime_plugin.WindowCommand):
    """Add file/folder to another project's sources"""

    def run(self, paths=None):
        if not paths:
            return

        self._path = paths[0]

        # Find all projects in open folders
        self._projects = []
        for folder in self.window.folders():
            for root, dirs, files in os.walk(folder):
                if '.rsyncproject' in files:
                    self._projects.append(
                        os.path.join(root, '.rsyncproject'))
                dirs[:] = [d for d in dirs if not d.startswith('.')]

        if not self._projects:
            sublime.error_message("No .rsyncproject found")
            return

        items = [
            [get_project_name(p), os.path.dirname(p)]
            for p in self._projects
        ]
        self.window.show_quick_panel(items, self._on_project_select)

    def _on_project_select(self, index):
        if index < 0:
            return

        rsyncproject = self._projects[index]
        root = get_project_root(rsyncproject)
        rel_path = os.path.relpath(self._path, root)

        config = load_rsyncproject(rsyncproject)
        if config is None:
            sublime.status_message("Error: Invalid JSON in .rsyncproject")
            return

        sources = config.get('sources', [])

        if rel_path in sources:
            sublime.status_message(f"'{rel_path}' already in sources")
            return

        sources.append(rel_path)
        config['sources'] = sources
        save_rsyncproject(rsyncproject, config)
        sublime.status_message(
            f"Added '{rel_path}' to {get_project_name(rsyncproject)} sources")

    def is_visible(self, paths=None):
        if not paths:
            return False
        for folder in self.window.folders():
            for root, dirs, files in os.walk(folder):
                if '.rsyncproject' in files:
                    return True
                dirs[:] = [d for d in dirs if not d.startswith('.')]
        return False


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
                        'rsync', f'RSYNC: {name} [{target_name}]')
                    return
            self.view.set_status('rsync', f'RSYNC: {name}')
        else:
            self.view.erase_status('rsync')


def plugin_loaded():
    """Called by Sublime when plugin is loaded"""
    print("RsyncTool: plugin loaded")


class RsyncEventListener(sublime_plugin.EventListener):
    """Event listener for automatic actions"""

    def on_activated(self, view):
        """Update status bar when switching tabs"""
        view.run_command('rsync_update_status')
