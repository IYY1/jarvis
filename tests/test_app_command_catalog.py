"""Every installed desktop entry receives a resolvable open/close command pair."""
import ast
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from murmur import config
from murmur.apps import AppRegistry
from murmur.commands import Parser, match_candidates

ROOT = Path(__file__).resolve().parents[1]

class ApplicationCatalogTests(unittest.TestCase):
    def make_registry(self, entries, aliases=None):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for identity, name in entries:
            (root / identity).write_text(f'[Desktop Entry]\nType=Application\nName={name}\nExec={identity[:-8]}\nStartupWMClass={identity[:-8]}\n')
        return AppRegistry(aliases, [root]), root

    def test_all_apps_have_both_commands_even_ambiguous_reserved_and_blocked_names(self):
        cfg = config.validate({'aliases': {'browser': 'browser.desktop'}, 'app_prefixes': {'browser': 'watch'}})
        parser = Parser.from_config(cfg)
        entries = [('browser.desktop', 'browser'), ('one.desktop', 'Settings'), ('two.desktop', 'Settings'),
                   ('brightness.desktop', 'brightness'), ('application.desktop', 'application one')]
        registry, root = self.make_registry(entries, cfg['aliases'])
        catalog = registry.voice_commands(parser)
        self.assertEqual({row['app_id'] for row in catalog}, set(registry.apps))
        self.assertEqual(len({row['voice_name'] for row in catalog}), len(entries))
        names = [*registry.index, *(row['voice_name'] for row in catalog)]
        grammar = parser.grammar(names)
        candidates = match_candidates(grammar)
        for row in catalog:
            self.assertEqual(row['examples'], ['open ' + row['voice_name'], 'close ' + row['voice_name']])
            for phrase, action in zip(row['examples'], ('open_app', 'close_app')):
                intent = parser.parse(phrase)
                self.assertEqual(intent.action, action)
                self.assertEqual(registry.resolve(intent.target).id, row['app_id'])
                self.assertIn(phrase, candidates)
        before = catalog
        self.assertFalse(registry.rescan())
        self.assertEqual(registry.voice_commands(parser), before)
        (root/'new.desktop').write_text('[Desktop Entry]\nType=Application\nName=Settings\nExec=new\n')
        self.assertTrue(registry.rescan())
        self.assertEqual(len(registry.voice_commands(parser)), len(entries) + 1)

    def test_display_names_precede_sorted_aliases_and_rescans_refresh_alternatives(self):
        aliases = {'aaa': 'primary.desktop', 'zulu': 'zz-a.desktop', 'alpha': 'zz-a.desktop'}
        registry, _ = self.make_registry([('primary.desktop', 'Primary'),
                                         ('zz-a.desktop', 'Shared'), ('zz-b.desktop', 'Shared')], aliases)
        parser = Parser()
        names = lambda: {row['app_id']: row['voice_name'] for row in registry.voice_commands(parser)}
        self.assertEqual(names(), {'primary.desktop': 'primary', 'zz-a.desktop': 'alpha',
                                   'zz-b.desktop': 'zz b'})
        aliases['aardvark'] = 'zz-a.desktop'
        registry.rescan()
        self.assertEqual(names()['zz-a.desktop'], 'aardvark')
        del aliases['aardvark']
        registry.rescan()
        self.assertEqual(names()['zz-a.desktop'], 'alpha')

    def test_native_catalog_and_grammar_match_python_for_every_entry(self):
        binary = ROOT/'native/controller/target/release/jarvis-controller'
        if not binary.exists(): self.skipTest('build native controller')
        from tests.test_rust_controller import policies
        cfg = config.validate({'aliases': {'browser': 'browser.desktop'}, 'app_prefixes': {'browser': 'watch'}})
        registry, root = self.make_registry([('browser.desktop','browser'),('one.desktop','Settings'),
            ('two.desktop','Settings'),('brightness.desktop','brightness'),('four.desktop','!!!')],cfg['aliases'])
        parser = Parser.from_config(cfg)
        native = policies([{'op':'apps','roots':[str(root)],'aliases':cfg['aliases'],'config':cfg}])[0]
        self.assertTrue(native['ok'],native)
        self.assertEqual(native['result']['applications'], registry.voice_commands(parser))
        candidates = match_candidates(native['result']['grammar'])
        requests=[]
        for row in native['result']['applications']:
            for phrase in row['examples']:
                self.assertIn(phrase,candidates)
                requests.extend([{'op':'parse','text':phrase,'config':cfg},
                    {'op':'apps','roots':[str(root)],'aliases':cfg['aliases'],'resolve':parser.parse(phrase).target}])
        results=policies(requests)
        for result in results: self.assertTrue(result['ok'],result)

    def test_service_catalog_covers_all_installed_apps_without_running_them(self):
        binary=ROOT/'native/controller/target/release/jarvis-controller'
        if not binary.exists(): self.skipTest('build native controller')
        from tests.test_rust_controller import Harness
        h=Harness();self.addCleanup(h.close)
        apps=h.root/'data/applications';apps.mkdir(parents=True)
        for i in range(95):
            (apps/f'program{i}.desktop').write_text(f'[Desktop Entry]\nType=Application\nName=Program {i}\nExec=program{i}\n')
        self.assertTrue(h.rpc('apps')['ok'])
        reply=h.rpc('commands');self.assertTrue(reply['ok'],reply)
        self.assertEqual(len(reply['applications']),95)
        self.assertEqual(len({row['app_id'] for row in reply['applications']}),95)
        self.assertTrue(all(len(row['examples'])==2 for row in reply['applications']))
        self.assertEqual(h.events('gio'),[])
        self.assertEqual(h.events('hyprctl'),[])

class CatalogPaginationTests(unittest.TestCase):
    def test_all_entries_are_browsable_and_searchable_with_only_thirty_cards(self):
        tree=ast.parse((ROOT/'src/murmur/ui.py').read_text())
        methods={'_render_commands','_filter_commands','_change_command_page','_render_command_page'}
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and any(isinstance(m,ast.FunctionDef) and m.name=='_render_commands' for m in n.body))
        module=ast.Module(body=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in methods],type_ignores=[])
        namespace={'Any':object,'_label':lambda value,*_:value}
        exec(compile(module,'ui-catalog-methods','exec'),namespace)
        class Card:
            def __init__(self,title): self.title=title;self.children=[]
            def append(self,item): self.children.append(item)
        class Control:
            def set_text(self,text): self.text=text
            def set_sensitive(self,enabled): self.enabled=enabled
        window=types.SimpleNamespace(_command_data=[],_command_page=0,_command_rows=[],command_list=[],
            command_search=types.SimpleNamespace(get_text=lambda:''),command_previous=Control(),command_next=Control(),command_count=Control())
        window._clear=lambda items:items.clear();window._card=lambda title,category:Card(title)
        for method in methods: setattr(window,method,types.MethodType(namespace[method],window))
        rows=[{'title':f'Program {i}','category':'Applications','app_id':f'app{i}',
               'examples':[f'open program {i}',f'close program {i}']} for i in range(95)]
        window._render_commands(rows)
        seen=[]
        while True:
            self.assertLessEqual(len(window.command_list),30)
            seen.extend(card.title for card in window.command_list)
            if not window.command_next.enabled:break
            window._change_command_page(1)
        self.assertEqual(seen,[row['title'] for row in rows])
        window.command_search.get_text=lambda:'program 94'
        window._filter_commands()
        self.assertEqual([card.title for card in window.command_list],['Program 94'])
        self.assertEqual(window.command_list[0].children,['“open program 94”','“close program 94”'])
        self.assertFalse(window.command_next.enabled)
        self.assertFalse(window.command_previous.enabled)
