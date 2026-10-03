"""公开版路径配置回归；所有读写均落在 tests/.artifacts 内。"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / '02_实现'))
from usage_core import Ledger, project_id


class Configuration(unittest.TestCase):
    def setUp(self):
        artifacts = TESTS / '.artifacts'
        artifacts.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='配置-', dir=artifacts)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / '看板仓库'
        self.root.mkdir()

    def config(self, value):
        (self.root / 'config.json').write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8-sig')

    def test_environment_and_default_workspace(self):
        home = self.base / '自定义 Codex 数据'
        with patch.dict(os.environ, {'CODEX_HOME': str(home)}):
            ledger = Ledger(self.root)
        self.assertEqual(ledger.codex_home, home)
        self.assertEqual(ledger.workspace, self.root.parent)
        self.assertIsNone(ledger.registry)
        self.assertEqual(ledger.catalog(), ({}, {}))

    def test_configuration_paths_relative_to_repository(self):
        self.config({'codex_home': '../日志', 'workspace': '../项目', 'registry': '项目登记.json', 'projects': ['../非标准名称项目']})
        with patch.dict(os.environ, {'CODEX_HOME': str(self.base / '不应使用')}):
            ledger = Ledger(self.root)
        self.assertEqual(ledger.codex_home, self.base / '日志')
        self.assertEqual(ledger.workspace, self.base / '项目')
        self.assertEqual(ledger.registry, self.root / '项目登记.json')
        self.assertEqual(ledger.project_paths, [self.base / '非标准名称项目'])
        catalog, _ = ledger.catalog()
        self.assertIn(project_id(self.base / '非标准名称项目'), catalog)

    def test_explicit_arguments_override_file(self):
        self.config({'codex_home': '配置日志', 'workspace': '配置项目', 'registry': '配置登记.json', 'projects': ['配置项目']})
        ledger = Ledger(self.root, codex_home=self.base / '参数日志', workspace=self.base / '参数项目', registry='', projects=[])
        self.assertEqual(ledger.codex_home, self.base / '参数日志')
        self.assertEqual(ledger.workspace, self.base / '参数项目')
        self.assertIsNone(ledger.registry)
        self.assertEqual(ledger.project_paths, [])

    def test_project_discovery_stops_after_one_category(self):
        workspace = self.base / '工作区'
        expected = [workspace / 'project_001_文档', workspace / '创作分类' / 'project_002_视频']
        ignored = [workspace / '分类' / '更深层' / 'project_003_不扫描', workspace / '.隐藏分类' / 'project_004_不扫描']
        for path in expected + ignored:
            path.mkdir(parents=True)
        custom = self.base / '其他命名项目'
        ledger = Ledger(self.root, codex_home=self.base / '空日志', workspace=workspace, projects=[str(custom)])
        catalog, links = ledger.catalog()
        self.assertEqual(set(catalog), {project_id(path) for path in expected + [custom]})
        self.assertEqual(links, {})

    def test_optional_registry_can_link_a_thread(self):
        registry = self.root / '项目登记.json'
        project = self.base / '登记项目'
        registry.write_text(json.dumps({'entries': [{'threadId': '合成会话', 'projectRoot': str(project)}]}), encoding='utf-8')
        ledger = Ledger(self.root, codex_home=self.base / '空日志', workspace=self.base / '空工作区', registry=registry)
        catalog, links = ledger.catalog()
        self.assertIn(project_id(project), catalog)
        self.assertEqual(links, {'合成会话': {project_id(project)}})

    def test_bad_configuration_fails_with_clear_error(self):
        for config in ([], {'workspace': 7}, {'projects': '不是列表'}, {'projects': ['']}, {'拼写错误': True}):
            with self.subTest(config=config):
                self.config(config)
                with self.assertRaises(ValueError):
                    Ledger(self.root)
        (self.root / 'config.json').write_text('{错误 JSON', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '有效的 UTF-8 JSON'):
            Ledger(self.root)


if __name__ == '__main__':
    unittest.main(verbosity=2)
