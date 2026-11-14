"""Source-distribution boundary checks; standard library only and no network."""
from pathlib import Path
import importlib.util
import sys
import unittest
import hashlib

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("public_release_under_test",ROOT/"ampuly/cli/release_package.py")
release=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=release
spec.loader.exec_module(release)

class PublicReleaseBoundaryTests(unittest.TestCase):
    def test_engine_source_is_included(self):
        self.assertTrue(release.public_source_path("ampuly/engine.py"))

    def test_required_public_files_exist(self):
        for path in release.REQUIRED_RELEASE_EVIDENCE:
            with self.subTest(path=path): self.assertTrue((ROOT/path).is_file())

    def test_runtime_homes_are_excluded(self):
        for path in (".ampuly/config.yaml",".opc/settings.json","ampuly/.ampuly/config.yaml"):
            with self.subTest(path=path): self.assertFalse(release.public_source_path(path))

    def test_keys_logs_and_databases_are_excluded(self):
        for suffix in (".pem",".key",".p12",".pfx",".db",".db-wal",".sqlite3",".jsonl",".log"):
            with self.subTest(suffix=suffix): self.assertFalse(release.public_source_path("ampuly/example"+suffix))

    def test_environment_files_are_excluded(self):
        for path in ("ampuly/.env","ampuly/config/.env.production"):
            with self.subTest(path=path): self.assertFalse(release.public_source_path(path))

    def test_internal_documents_are_excluded(self):
        for path in ("docs/evidence/result.md","docs/collab/notes.md","ENGINE-PLAN.md","ampuly/AGENTS.md"):
            with self.subTest(path=path): self.assertFalse(release.public_source_path(path))

    def test_business_assets_are_not_in_allowlist(self):
        for path in ("distribution/workflows/private/workflow.yaml","distribution/company_orgs/private.yaml","distribution/skills/private/SKILL.md"):
            with self.subTest(path=path): self.assertFalse(release.public_source_path(path))

    def test_old_frontend_build_is_excluded(self):
        self.assertFalse(release.public_source_path("ampuly/plugins/office_ui/frontend_dist/index.html"))

    def test_traversal_and_nested_backups_are_excluded(self):
        for path in ("../private.txt","ampuly/../private.py","/ampuly/example.py","ampuly/.backups/secret.yaml","ampuly/node_modules/example.js","ampuly/sessions/example.txt"):
            with self.subTest(path=path): self.assertFalse(release.public_source_path(path))

    def test_single_public_role_template_is_included(self):
        self.assertTrue(release.public_source_path("distribution/prompts/talent/general-default-employee.md"))

    def test_upstream_mit_preserved_verbatim(self):
        data = (ROOT/'licenses/OpenOPC-MIT-LICENSE').read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), '67cf8409879d9c7dae326bed46582f5a26390e4f4669504ca42d8d4131455c00')

    def test_license_and_sponsorship_are_shipped(self):
        for path in ('LICENSE', 'licenses/OpenOPC-MIT-LICENSE', 'SPONSORSHIP.zh.md'):
            with self.subTest(path=path): self.assertTrue(release.public_source_path(path))

if __name__=="__main__": unittest.main()
