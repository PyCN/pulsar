import os
import sys
import logging
import subprocess
from itertools import chain
from asyncio import get_event_loop

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:     # pragma    nocover
    Observer = FileSystemEventHandler = None

LOGGER = logging.getLogger('pulsar.autoreload')
EXIT_CODE = 5
PULSAR_RUN_MAIN = "PULSAR_RUN_MAIN"


class Reloader:
    name = None

    def __init__(self, extra_files=None, interval=1):
        self.extra_files = set(os.path.abspath(x) for x in extra_files or ())
        self.interval = interval or 1
        self._loop = get_event_loop()

    def start(self):
        self.run()

    def run(self):
        pass

    def sleep(self):
        if not self._loop.is_closed():
            self._loop.call_later(self.interval, self.run)

    def is_closed(self):
        return self._loop.is_closed()

    def restart_with_reloader(self):
        """Spawn a new Python interpreter with the same arguments as this one
        """
        while True:
            LOGGER.info('Restarting with %s reloader' % self.name)
            args = _get_args_for_reloading()
            new_environ = os.environ.copy()
            new_environ[PULSAR_RUN_MAIN] = 'true'
            exit_code = subprocess.call(args, env=new_environ, close_fds=False)
            if exit_code != EXIT_CODE:
                return exit_code

    def trigger_reload(self, filename):
        if self.log_reload(filename):
            self.exit()

    def log_reload(self, filename):
        if not self.is_closed():
            filename = os.path.abspath(filename)
            LOGGER.info('Detected change in %r, reloading', filename)
            return True

    def exit(self):
        sys.exit(EXIT_CODE)


class StatReloader(Reloader):
    name = 'stat'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mtimes = {}

    def run(self):
        for filename in chain(_iter_module_files(), self.extra_files):
            try:
                mtime = os.stat(filename).st_mtime
            except OSError:
                continue

            old_time = self.mtimes.get(filename)
            if old_time is None:
                self.mtimes[filename] = mtime
                continue
            elif mtime > old_time:
                return self.trigger_reload(filename)
        self.sleep()


class WatchdogReloader(Reloader):
    name = 'watchdog'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.observable_paths = set()
        self.observer = Observer()
        self.event_handler = CustomHandler(self)
        self.watches = {}
        self.should_reload = False

    def trigger_reload(self, filename):
        # This is called inside an event handler, which means throwing
        # SystemExit has no effect.
        # https://github.com/gorakhargosh/watchdog/issues/294
        if self.log_reload(filename):
            self.should_reload = True

    def start(self):
        self.observer.start()
        self.run()

    def run(self):
        if self.should_reload:
            self.exit()
        if self.is_closed():
            return
        to_delete = set(self.watches)
        paths = _find_observable_paths(self.extra_files)
        for path in paths:
            if path not in self.watches:
                try:
                    self.watches[path] = self.observer.schedule(
                        self.event_handler, path, recursive=True)
                except OSError:
                    # Clear this path from list of watches We don't want
                    # the same error message showing again in the next
                    # iteration.
                    self.watches[path] = None
            to_delete.discard(path)
        for path in to_delete:
            watch = self.watches.pop(path, None)
            if watch is not None:
                self.observer.unschedule(watch)
        self.observable_paths = paths
        self.sleep()

    def check_modification(self, filename):
        if filename in self.extra_files:
            self.trigger_reload(filename)
        dirname = os.path.dirname(filename)
        if dirname.startswith(tuple(self.observable_paths)):
            if filename.endswith(('.pyc', '.pyo')):
                self.trigger_reload(filename[:-1])
            elif filename.endswith('.py'):
                self.trigger_reload(filename)


def start(reloader_type='auto', interval=None):
    reloader = reloaders[reloader_type](interval=interval)
    try:
        if os.environ.get(PULSAR_RUN_MAIN) == "true":
            reloader.run()
        else:
            sys.exit(reloader.restart_with_reloader())
    except KeyboardInterrupt:
        pass


reloaders = dict(
    stat=StatReloader
)

if Observer:
    reloaders['watchdog'] = WatchdogReloader
    reloaders['auto'] = reloaders['watchdog']

    class CustomHandler(FileSystemEventHandler):

        def __init__(self, reloader):
            self.reloader = reloader
            super().__init__()

        def on_created(self, event):
            self.reloader.check_modification(event.src_path)

        def on_modified(self, event):
            self.reloader.check_modification(event.src_path)

        def on_moved(self, event):
            self.reloader.check_modification(event.src_path)
            self.reloader.check_modification(event.dest_path)

        def on_deleted(self, event):
            self.reloader.check_modification(event.src_path)

else:   # pragma    nocover
    reloaders['auto'] = reloaders['stat']


# INTERNALS

def _get_args_for_reloading():
    """Returns the executable. This contains a workaround for windows
    if the executable is incorrectly reported to not have the .exe
    extension which can cause bugs on reloading.
    """
    rv = [sys.executable]
    py_script = sys.argv[0]
    if os.name == 'nt' and not os.path.exists(py_script) and \
       os.path.exists(py_script + '.exe'):
        py_script += '.exe'
    rv.append(py_script)
    rv.extend(sys.argv[1:])
    return rv


def _iter_module_files():
    """This iterates over all relevant Python files.  It goes through all
    loaded files from modules, all files in folders of already loaded modules
    as well as all files reachable through a package.
    """
    # The list call is necessary on Python 3 in case the module
    # dictionary modifies during iteration.
    for module in list(sys.modules.values()):
        if module is None:
            continue
        filename = getattr(module, '__file__', None)
        if filename:
            while not os.path.isfile(filename):
                old = filename
                filename = os.path.dirname(filename)
                if filename == old:
                    break
            else:
                if filename[-4:] in ('.pyc', '.pyo'):
                    filename = filename[:-1]
                yield filename


def _find_observable_paths(extra_files=None):
    """Finds all paths that should be observed."""
    rv = set(os.path.abspath(x) for x in sys.path)

    for filename in extra_files or ():
        rv.add(os.path.dirname(os.path.abspath(filename)))

    for module in list(sys.modules.values()):
        fn = getattr(module, '__file__', None)
        if fn is None:
            continue
        fn = os.path.abspath(fn)
        rv.add(os.path.dirname(fn))

    return _find_common_roots(rv)


def _find_common_roots(paths):
    """Out of some paths it finds the common roots that need monitoring."""
    paths = [x.split(os.path.sep) for x in paths]
    root = {}
    for chunks in sorted(paths, key=len, reverse=True):
        node = root
        for chunk in chunks:
            node = node.setdefault(chunk, {})
        node.clear()

    rv = set()

    def _walk(node, path):
        for prefix, child in node.items():
            _walk(child, path + (prefix,))
        if not node:
            rv.add('/'.join(path))
    _walk(root, ())
    return rv
