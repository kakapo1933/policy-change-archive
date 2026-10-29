import copy
from datetime import datetime, timezone, timedelta
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from http.client import IncompleteRead
from urllib.error import HTTPError
import monitor

class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.targets = {"sites":[{"id":"one","name":"站一","homepage":"https://example.org","sources":[{"id":"homepage","url":"https://example.org","type":"html"}]},{"id":"two","name":"站二","homepage":"https://example.net","sources":[{"id":"homepage","url":"https://example.net","type":"json"}]}]}
        (self.dir/'targets.json').write_text(json.dumps(self.targets),encoding='utf-8')
        self.patch_root = patch.object(monitor,'ROOT',self.dir/'data')
        self.patch_config = patch.object(monitor,'CONFIG',self.dir/'targets.json')
        self.patch_base = patch.object(monitor,'BASE',self.dir)
        self.patch_root.start();self.patch_config.start();self.patch_base.start()

    def tearDown(self):
        self.patch_root.stop();self.patch_config.stop();self.patch_base.stop();self.tmp.cleanup()

    def test_initial_unchanged_changed_failure_and_recovery(self):
        date=datetime(2026,10,29,tzinfo=timezone.utc)
        values={'https://example.org':b'<html><h1>Policy A</h1><script>var t=123</script></html>','https://example.net':b'{"foo":1,"bar":2}'}
        def getter(url):
            if isinstance(values[url],Exception):raise values[url]
            return values[url],200,{'content-type':'text/html'}
        first=monitor.run(date,getter);self.assertEqual([r['change'] for r in first],['initial','initial'])
        self.assertNotIn('var t',monitor.normalized_text(values['https://example.org'],'html'))
        second=monitor.run(date+timedelta(days=1),getter);self.assertEqual([r['change'] for r in second],['unchanged','unchanged'])
        values['https://example.org']=b'<html><h1>Policy B</h1></html>'
        values['https://example.net']=ValueError('simulated outage')
        third=monitor.run(date+timedelta(days=2),getter)
        self.assertEqual(third[0]['change'],'changed');self.assertTrue(third[0]['diff'])
        self.assertEqual(third[1]['status'],'error');self.assertEqual(third[1]['previous_observed_at'],second[1]['observed_at'])
        values['https://example.net']=b'{"bar":2,"foo":1}'
        fourth=monitor.run(date+timedelta(days=3),getter)
        self.assertEqual(fourth[1]['status'],'ok');self.assertEqual(fourth[1]['change'],'changed')  # raw changed, normalized text identical
        self.assertEqual(fourth[1]['text_sha256'],first[1]['text_sha256'])
        h=json.loads((self.dir/'data/history.json').read_text())
        self.assertEqual(len(h),8)
        self.assertEqual(len(list((self.dir/'data/runs').glob('*.json'))),4)
        self.assertEqual(len(list((self.dir/'data/snapshots').glob('*.gz'))),4)

    def test_removed_restored_removed_again(self):
        date=datetime(2026,10,29,tzinfo=timezone.utc)
        state={'up':True}
        def getter(url):
            if url=='https://example.org' and not state['up']:
                raise HTTPError(url,404,'Not Found',{},None)
            return b'<p>page</p>' if url=='https://example.org' else b'{"a":1}',200,{}
        changes=[]
        for day,up in enumerate([True,False,False,True,False]):
            state['up']=up
            changes.append(monitor.run(date+timedelta(days=day),getter)[0]['change'])
        self.assertEqual(changes,['initial','removed','still_gone','restored','removed'])
        s=json.loads((self.dir/'data/summary.json').read_text())
        self.assertEqual(s['stats']['one']['change_events'],3)
        self.assertEqual([r['change'] for r in s['events'] if r['site_id']=='one'],['removed','restored','removed','initial'])

    def test_incomplete_read_does_not_abort_run(self):
        date=datetime(2026,10,29,tzinfo=timezone.utc)
        def getter(url):
            if url=='https://example.net':
                raise IncompleteRead(b'partial')
            return b'<p>ok</p>',200,{}
        records=monitor.run(date,getter)
        self.assertEqual([r['status'] for r in records],['ok','error'])
        self.assertIn('IncompleteRead',records[1]['error'])

    def test_duplicate_run_rejected(self):
        date=datetime(2026,10,29,tzinfo=timezone.utc)
        get=lambda url:(b'<main>Hi</main>',200,{})
        monitor.run(date,get)
        with self.assertRaisesRegex(ValueError,'run_id already exists'):
            monitor.run(date,get)

    def test_policy_text_excludes_urls_and_includes_claims(self):
        blob = json.dumps({"meta":{"source":"https://unwanted.test","updated":"2026-10-29"}, "items":[{"id":"abc","title":"捷運建設推動計畫","detail":"增加公共交通運量改善城市移動"}]}).encode()
        content = monitor.policy_text(blob)
        self.assertIn("捷運建設推動計畫", content)
        self.assertNotIn("https://unwanted.test", content)
        self.assertNotIn("2026-10-29", content)

    def test_bad_html_error_handled(self):
        self.assertIn('one',monitor.normalized_text(b'<p>one</p><script>two</script>','html'))
        self.assertNotIn('two',monitor.normalized_text(b'<p>one</p><script>two</script>','html'))
        self.assertIsNone(monitor.similarity('short','abc'))

if __name__ == '__main__': unittest.main()
