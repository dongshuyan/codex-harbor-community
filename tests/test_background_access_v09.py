"""Background reads must not probe unrelated storage paths.

Every path is synthetic. These tests observe file-system calls, not macOS TCC:
they never launch Codex, read account credentials, or change system privacy state.
"""
import builtins
from contextlib import ExitStack, contextmanager
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import core
import main


@contextmanager
def accesses_under(root):
    """Record real metadata/content access beneath a temporary fixture path."""
    accesses = []
    prefix = str(root) + os.sep

    def observe(original, name):
        def call(path, *args, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)):
                raw = os.fsdecode(os.fspath(path))
                lexical = os.path.abspath(raw)
                if lexical == str(root) or lexical.startswith(prefix):
                    accesses.append((name, lexical))
            return original(path, *args, **kwargs)
        return call

    with ExitStack() as stack:
        for module, names in ((os, ('stat', 'lstat', 'access', 'open', 'scandir', 'listdir', 'readlink')),
                              (builtins, ('open',)), (io, ('open',))):
            for name in names:
                stack.enter_context(patch.object(module, name, observe(getattr(module, name), name)))
        yield accesses


class BackgroundAccessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='harbor-background-access-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = core.Store(self.root/'control', self.root/'external',
                                mounted=lambda: False, community=True)

    def write_registry(self, *, storages=(), removed=()):
        core.write_json(self.store.registry.path, {'revision': 1,
                         'storages': list(storages), 'removed_storages': list(removed)})

    def assert_no_access(self, accesses):
        readable = [(operation, str(Path(path).relative_to(self.root)))
                    for operation, path in accesses]
        self.assertEqual(readable, [], 'Background work probed an unrelated storage path')

    def test_status_does_not_probe_a_disabled_desktop_history(self):
        desktop = self.root/'synthetic-user'/'Desktop'
        history = desktop/'unused-history'
        history.mkdir(parents=True)
        self.write_registry(storages=[dict(id='d'*32, name='Disabled history',
                            path=str(history), enabled=False, layout='native')])

        with accesses_under(desktop) as accesses:
            result = main.status(self.store)

        self.assertEqual(result['storages'][0]['id'], 'd'*32)
        self.assertFalse(result['storages'][0]['available'])
        self.assertFalse(result['storages'][0]['exists'])
        self.assertEqual(result['storages'][0]['reason'], '已停用')
        self.assert_no_access(accesses)

    def test_status_does_not_probe_removed_history_even_if_it_was_enabled(self):
        removable = self.root/'synthetic-removable'
        history = removable/'removed-history'
        history.mkdir(parents=True)
        self.write_registry(removed=[dict(id='e'*32, name='Removed history',
                            path=str(history), enabled=True, layout='native')])

        with accesses_under(removable) as accesses:
            result = main.status(self.store)

        self.assertEqual(result['removed_storages'][0]['id'], 'e'*32)
        self.assertTrue(result['removed_storages'][0]['enabled'])
        self.assertFalse(result['removed_storages'][0]['available'])
        self.assertFalse(result['removed_storages'][0]['exists'])
        self.assertEqual(result['removed_storages'][0]['reason'], '已移除')
        self.assert_no_access(accesses)

    def test_enabled_history_still_rejects_a_path_replaced_with_a_symlink(self):
        history = self.root/'enabled-history'
        history.mkdir()
        ident = 'a'*32
        self.write_registry(storages=[dict(id=ident, name='Enabled history',
                            path=str(history), enabled=True, layout='native')])
        before = self.store.storage_snapshot()['storages'][0]
        self.assertTrue(before['exists'])
        self.assertTrue(before['available'])
        self.assertEqual(self.store.select_storage(ident), ident)

        moved = self.root/'moved-history'
        history.rename(moved)
        history.symlink_to(moved, target_is_directory=True)

        after = self.store.storage_snapshot()['storages'][0]
        self.assertFalse(after['available'])
        with self.assertRaises(core.UserError):
            self.store.select_storage(ident)

    def test_store_construction_does_not_probe_unselected_external_storage(self):
        removable = self.root/'synthetic-removable'
        external = removable/'never-selected'
        external.mkdir(parents=True)

        with accesses_under(removable) as accesses:
            store = core.Store(self.root/'other-control', external,
                               mounted=lambda: False, community=True)

        self.assertEqual(store.external, external)
        self.assert_no_access(accesses)

    def test_external_symlinks_are_rejected_when_selected_or_initialized(self):
        target = self.root/'external-target'
        target.mkdir()
        alias = self.root/'external-alias'
        alias.symlink_to(target, target_is_directory=True)
        for index, external in enumerate((alias, alias/'new-storage')):
            with self.subTest(external=external.name):
                store = core.Store(self.root/f'symlink-control-{index}', external,
                                   mounted=lambda: True, community=True)
                core.write_json(store.registry.path, {'revision': 1, 'storages': [
                    dict(id='external', name='External', path=str(external),
                         enabled=True, layout='harbor')]})

                with self.assertRaises(core.UserError):
                    store.select_storage('external')
                with self.assertRaises(core.UserError):
                    store.initialize_storage('external')
                self.assertEqual(list(target.iterdir()), [])

    def test_status_does_not_check_external_volume_without_a_registration(self):
        removable = self.root/'synthetic-removable'
        removable.mkdir()
        self.write_registry()
        # Model the mount check with a real access to a synthetic disk path.
        self.store.mounted = removable.is_dir

        with accesses_under(removable) as accesses:
            result = main.status(self.store)

        self.assertEqual(result['storages'], [])
        self.assert_no_access(accesses)


if __name__ == '__main__':
    unittest.main()
