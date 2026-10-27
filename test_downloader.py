import ast
import io
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).with_name('steam_downloader.py')
tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
ns = dict(os=os, shutil=shutil, subprocess=subprocess, tempfile=tempfile,
          threading=threading, queue=queue, requests=Mock(), tk=Mock())
functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
app = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'SteamDownloaderApp')
methods = [n for n in app.body if isinstance(n, ast.FunctionDef)]
exec(compile(ast.Module(body=functions + methods, type_ignores=[]), str(SOURCE), 'exec'), ns)


class RegressionTests(unittest.TestCase):
    def test_idle_stop_does_not_change_button(self):
        app = Mock(_downloading=False)
        ns['_stop_download'](app)
        app.stop_btn.config.assert_not_called()

    def test_failed_collection_result_is_rejected(self):
        response = Mock(status_code=200)
        response.json.return_value = {'response': {'collectiondetails': [
            {'result': 9, 'children': [{'publishedfileid': '123'}]}]}}
        ns['requests'].post.return_value = response
        items, error = ns['get_collection_items']('10')
        self.assertEqual(items, [])
        self.assertTrue(error)

    def test_nested_collection_is_not_downloaded_as_mod(self):
        response = Mock(status_code=200)
        response.json.return_value = {'response': {'collectiondetails': [
            {'result': 1, 'children': [
                {'publishedfileid': '123', 'filetype': 0},
                {'publishedfileid': '456', 'filetype': 2}]}]}}
        ns['requests'].post.return_value = response
        items, error = ns['get_collection_items']('10')
        self.assertEqual(items, ['123'])
        self.assertTrue(error)  # Explain unsupported nested collection instead of silent omission.

    def test_close_waits_for_worker_before_destroy(self):
        app = Mock(_downloading=True)
        app._worker_thread.is_alive.return_value = True
        self.assertIn('_close', ns)
        ns['_close'](app)
        app._stop_event.set.assert_called_once()
        app.root.destroy.assert_not_called()
        app.root.after.assert_called_once()

    def test_mod_update_replaces_old_files(self):
        self.assertIn('replace_mod_directory', ns)
        with tempfile.TemporaryDirectory(dir=SOURCE.parent) as folder:
            src, dest = Path(folder)/'src', Path(folder)/'dest'
            src.mkdir(); dest.mkdir()
            (src/'new').write_text('new')
            (dest/'obsolete').write_text('old')
            ns['replace_mod_directory'](str(src), str(dest))
            self.assertEqual([p.name for p in dest.iterdir()], ['new'])

    def test_copy_failure_preserves_previous_mod(self):
        self.assertIn('replace_mod_directory', ns)
        with tempfile.TemporaryDirectory(dir=SOURCE.parent) as folder:
            src, dest = Path(folder)/'src', Path(folder)/'dest'
            src.mkdir(); dest.mkdir()
            (dest/'old').write_text('keep')
            with patch.object(shutil, 'copytree', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    ns['replace_mod_directory'](str(src), str(dest))
            self.assertEqual((dest/'old').read_text(), 'keep')

    def test_publish_failure_rolls_back_previous_mod(self):
        with tempfile.TemporaryDirectory(dir=SOURCE.parent) as folder:
            src, dest = Path(folder)/'src', Path(folder)/'dest'
            src.mkdir(); dest.mkdir()
            (src/'new').write_text('new')
            (dest/'old').write_text('keep')
            replace = os.replace

            def fail_publish(a, b):
                if Path(a).name == 'new':
                    raise OSError('destination locked')
                return replace(a, b)

            with patch.object(os, 'replace', side_effect=fail_publish):
                with self.assertRaises(OSError):
                    ns['replace_mod_directory'](str(src), str(dest))
            self.assertEqual((dest/'old').read_text(), 'keep')

    def test_progress_arrives_before_process_finishes(self):
        progress = []

        class Output:
            def __iter__(output):
                yield 'Success. Downloaded item 123\n'
                self.assertEqual(progress, [99])
                yield 'progress: 100\n'

            def close(output):
                pass

        with tempfile.TemporaryDirectory(dir=SOURCE.parent) as folder:
            src = Path(folder)/'steamapps/workshop/content/294100/123'
            src.mkdir(parents=True)
            (src/'mod').write_text('data')
            marker = Path(folder)/'steamapps/keep'
            marker.write_text('keep')
            proc = Mock(stdout=Output(), returncode=0)
            proc.poll.return_value = 0
            logs = []
            with patch.object(subprocess, 'Popen', return_value=proc):
                result = ns['download_mods']('294100', ['123'], folder, 'steamcmd.exe',
                    lambda m, t: logs.append(m), progress.append)
            self.assertTrue(result)
            self.assertEqual(progress, [99, 100])
            self.assertTrue(marker.exists())
            self.assertEqual((Path(folder)/'123/mod').read_text(), 'data')
            self.assertFalse(any('progress:' in m for m in logs))

    def test_blocked_rollback_keeps_recoverable_backup(self):
        with tempfile.TemporaryDirectory(dir=SOURCE.parent) as folder:
            src, dest = Path(folder)/'src', Path(folder)/'dest'
            src.mkdir(); dest.mkdir()
            (dest/'old').write_text('keep')
            replace = os.replace

            def fail_after_backup(a, b):
                if Path(a).name in ('new', 'previous'):
                    raise OSError('locked')
                return replace(a, b)

            with patch.object(os, 'replace', side_effect=fail_after_backup):
                with self.assertRaises(OSError):
                    ns['replace_mod_directory'](str(src), str(dest))
            backups = list(Path(folder).glob('.mod-transfer-*/previous/old'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), 'keep')

    def test_failed_process_never_reports_success(self):
        with tempfile.TemporaryDirectory(dir=SOURCE.parent) as folder:
            proc = Mock(stdout=io.StringIO('ERROR! Download failed\n'), returncode=1)
            proc.poll.return_value = 1
            progress, logs = [], []
            with patch.object(subprocess, 'Popen', return_value=proc):
                result = ns['download_mods']('294100', ['123'], folder, 'steamcmd.exe',
                    lambda m, t: logs.append((m, t)), progress.append)
            self.assertFalse(result)
            self.assertNotIn(100, progress)
            self.assertFalse(any(t == 'success' for _, t in logs))

    def test_close_destroys_window_after_worker_exits(self):
        app = Mock(_downloading=True)
        app._worker_thread.is_alive.return_value = False
        ns['_close'](app)
        app.root.destroy.assert_called_once()

    def test_stop_terminates_silent_process(self):
        event, terminated = threading.Event(), threading.Event()

        class Process:
            returncode = None

            def __init__(self):
                self.stdout = self

            def __iter__(self):
                event.set()
                if not terminated.wait(2):
                    raise RuntimeError('process was not terminated')
                return iter(())

            def terminate(self):
                self.returncode = 1
                terminated.set()

            def wait(self, timeout=None):
                return self.returncode

            def poll(self):
                return self.returncode

            def close(self):
                pass

        with tempfile.TemporaryDirectory(dir=SOURCE.parent) as folder:
            with patch.object(subprocess, 'Popen', return_value=Process()):
                result = ns['download_mods']('294100', ['123'], folder, 'steamcmd.exe',
                    lambda *args: None, lambda v: None, stop_event=event)
            self.assertFalse(result)
            self.assertTrue(terminated.is_set())


if __name__ == '__main__':
    unittest.main()
