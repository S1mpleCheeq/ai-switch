import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
import uuid
import zipfile

import release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='ai-switch-release-test-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()/'source'
        self.root.mkdir()
        source = Path(__file__).parent
        for name in release.FILES:
            target = self.root/name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source/name, target)

    def test_archive_excludes_runtime_state_and_is_reproducible(self):
        private = self.root/'profiles/private/codex.json'
        private.parent.mkdir(parents=True)
        marker = 'private-fixture-'+uuid.uuid4().hex
        private.write_text(marker)
        (self.root/'.env').write_text('PRIVATE='+marker)
        archive, count = release.build(self.root, self.root/'dist')
        first = archive.read_bytes()
        with tarfile.open(archive) as bundle:
            files = {member.name.split('/', 1)[1] for member in bundle.getmembers()}
            self.assertEqual(files, set(release.FILES))
            for member in bundle.getmembers():
                self.assertNotIn(marker.encode(), bundle.extractfile(member).read())
        self.assertEqual(count, len(release.FILES))
        self.assertEqual(release.build(self.root, self.root/'dist')[0].read_bytes(), first)
        self.assertTrue(archive.with_name(archive.name+'.sha256').read_text().startswith(hashlib.sha256(first).hexdigest()))

    def test_key_findings_fail_without_echoing_value(self):
        key = 'sk-'+'privatefakevalue'*3
        (self.root/'README.md').write_text('accidental credential '+key)
        with self.assertRaises(ValueError) as result:
            release.collect(self.root)
        self.assertNotIn(key, str(result.exception))
        self.assertIn('README.md:1', str(result.exception))

    def test_platform_archives_contain_the_same_public_sources(self):
        for platform in ('linux', 'macos', 'windows'):
            archive, count = release.build(self.root, self.root/'dist', platform)
            if platform == 'windows':
                with zipfile.ZipFile(archive) as bundle:
                    content = {name.split('/',1)[1]:bundle.read(name) for name in bundle.namelist()}
            else:
                with tarfile.open(archive) as bundle:
                    content = {m.name.split('/',1)[1]:bundle.extractfile(m).read() for m in bundle.getmembers()}
            self.assertEqual(set(content), set(release.FILES))
            for name, data in content.items():
                self.assertEqual(data, (self.root/name).read_bytes())

    def test_arbitrary_template_key_and_symlinks_are_rejected(self):
        path = self.root/'templates/aster/claude.json'
        original = path.read_text()
        value = json.loads(original)
        value['env']['ANTHROPIC_AUTH_TOKEN'] = 'nonstandard-credential'
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, '占位符'):
            release.collect(self.root)
        path.write_text(original)
        path.unlink()
        path.symlink_to(self.root/'templates/aster/codex.json')
        with self.assertRaisesRegex(ValueError, '符号链接'):
            release.collect(self.root)


if __name__ == '__main__':
    unittest.main()
