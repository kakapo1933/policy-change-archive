import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import discovery
import monitor


class DiscoveryTests(unittest.TestCase):
    def test_canonical_scope_and_security(self):
        root = "https://puma.taipei"
        allowed = ["/policies", "/heart-island"]
        self.assertEqual(discovery.canonical(root + "/policies/運動政策#sport", "puma.taipei", allowed), root + "/policies/%E9%81%8B%E5%8B%95%E6%94%BF%E7%AD%96")
        for url in ["https://elsewhere.taipei/policies/x", "https://puma.taipei/admin/edit", "https://puma.taipei/policies/../../admin", "https://puma.taipei/policies/?page=100", "javascript:alert(1)", "https://puma.taipei/policies/document.pdf", "http://puma.taipei/policies/"]:
            self.assertIsNone(discovery.canonical(url, "puma.taipei", allowed), url)

    def test_discover_links_sitemap_assets_and_robots(self):
        home = "https://demo.example/"
        pages = {
            home + "robots.txt": b"User-agent: *\nDisallow: /private/\nSitemap: https://demo.example/sitemap.xml\n",
            home + "sitemap.xml": b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://demo.example/policies/alpha</loc></url></urlset>',
            home: b'<a href="/new/">New</a><a href="https://foreign.example/danger">Outside</a><a href="/private/secret">No</a><a href="/admin/logout">No</a><script src="/assets/app.js"></script>',
            home + "new/": b'<a href="/policies/alpha">A</a><a href="/new/?id=2">No query</a>',
            home + "policies/alpha": '<h1>政策甲</h1>'.encode(),
            home + "assets/app.js": b'const x = 1;',
        }
        def getter(url):
            if url not in pages:
                raise HTTPError(url, 404, 'Not Found', {}, None)
            ct = 'text/html' if url in {home, home+'new/', home+'policies/alpha'} else 'text/plain'
            return pages[url], 200, {'content-type': ct}
        target = {'id':'demo','homepage':home,'sources':[{'id':'homepage','url':home,'type':'html'}], 'crawl':{'discover_web':True,'allowed_paths':['/'],'max_pages':12,'max_depth':2,'delay_seconds':0}}
        found = discovery.discover(target,getter,[],sleep=lambda n: None)
        urls = {r['url'] for r in found.sources}
        self.assertEqual(urls,{home+'new/',home+'policies/alpha',home+'assets/app.js'})
        self.assertTrue(found.coverage['complete'])
        self.assertEqual(found.coverage['discovered_pages'],3)
        self.assertNotIn(home+'private/secret', urls)

    def test_robots_fail_closed(self):
        target = {'id':'demo','homepage':'https://demo.example/','sources':[], 'crawl':{'discover_web':True,'allowed_paths':['/'],'max_pages':10, 'delay_seconds':0}}
        def getter(url):
            raise HTTPError(url, 403, 'Forbidden', {}, None)
        result = discovery.discover(target, getter, [],sleep=lambda n:None)
        self.assertFalse(result.coverage['complete'])
        self.assertFalse(result.sources)
        self.assertIn('robots.txt',result.coverage['errors'][0])

    def test_virtual_policy_records_and_missing(self):
        root = 'https://demo.example'
        doc_url=root+'/policy.json'
        dataset={'source_id':'policies','kind':'heart-policies','policy_urls':True}
        site={'id':'demo','homepage':root+'/','sources':[{'id':'policies','type':'json','url':doc_url}], 'crawl':{'policy_datasets':[dataset], 'allowed_paths':['/']}}
        current={'sources':[{'url':root+'/policies/運動政策'}], 'islands':[{'id':'sport','buildings':[{'id':'run','name':'跑步政策','description':'每人每天三十分鐘'}, {'id':'gym','name':'健身政策','description':'全民健身'}]}]}
        def getter(url): return discovery.body_from_json(current),200,{'content-type':'application/json'}
        initial=discovery.discover(site,getter,[])
        self.assertEqual(len(initial.virtual_blobs),2)
        self.assertIn(root+'/policies/%E9%81%8B%E5%8B%95%E6%94%BF%E7%AD%96',{x['url'] for x in initial.sources})
        assert all(x['id'].startswith('item-') for x in initial.sources if x.get('logical'))
        past=[{'site_id':'demo','source_id':x['id'],'url':x['url'],'name':x['name'], 'status':'ok','discovered_by':x['discovered_by']} for x in initial.sources if x.get('logical')]
        current['islands'][0]['buildings'].pop()
        updated=discovery.discover(site,getter,past)
        self.assertEqual(len(updated.missing_virtual),1)
        self.assertIn('gym', updated.missing_virtual[0]['url'])

    def test_bilingual_policy_titles_are_human_readable(self):
        root = 'https://demo.example'
        url = root + '/policies.json'
        site = {'id':'demo', 'homepage':root+'/', 'sources':[{'id':'policies','url':url,'type':'json'}],
                'crawl':{'policy_datasets':[{'source_id':'policies','kind':'heart-policies'}]}}
        data = {'islands':[{'buildings':[{'id':'bus','name':{'zh':'公車安全改革','en':'Bus safety'}, 'detail':'改善公車政策'}]}]}
        result = discovery.discover(site, lambda u: (discovery.body_from_json(data), 200, {}), [])
        self.assertEqual(result.sources[0]['name'], '公車安全改革')

    def test_repo_manifest_filter(self):
        root='https://raw.githubusercontent.com/demo/example/main'
        tree='https://api.github.com/repos/demo/example/git/trees/main?recursive=1'
        site={'id':'demo','homepage':'https://github.com/demo/example','sources':[], 'crawl':{'repository_tree':tree,'repository_raw_base':root,'max_repo_files':10}}
        entries=[{'type':'blob','path':p,'size':10} for p in ['index.html','data/policies.json','main.js','docs/cover.html','vendor/lib.js','assets/photo.png','tests/test.js']]
        result=discovery.discover(site,lambda url:(json.dumps({'tree':entries}).encode(),200,{}),[])
        self.assertEqual({s['name'] for s in result.sources},{'index.html','data/policies.json','main.js'})
        self.assertEqual(result.coverage['repository_files'],3)


class CrawlIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.patches=[patch.object(monitor, 'BASE',self.root), patch.object(monitor,'ROOT',self.root/'data'), patch.object(monitor,'CONFIG',self.root/'targets.json')]
        for p in self.patches:p.start()
        self.url='https://example.test/'
        self.policy=self.url+'data/policies.json'
        self.data={'items':[{'id':'alpha','title':'運動驛站','detail':'新增公立運動設施'}]}
        self.paths={self.url+'robots.txt':b'User-agent: *\nAllow: /',self.url+'sitemap.xml':b'<urlset><url><loc>https://example.test/one/</loc></url></urlset>',self.url:b'<a href="/one/">Page</a>',self.url+'one/':b'<h1>My policy</h1>'}
        self.targets={'sites':[{'id':'one','name':'Example','homepage':self.url,'sources':[{'id':'homepage','url':self.url,'type':'html'},{'id':'policies','url':self.policy,'type':'json'}],'crawl':{'discover_web':True,'sitemaps':[self.url+'sitemap.xml'],'allowed_paths':['/'],'max_pages':12,'delay_seconds':0,'policy_datasets':[{'source_id':'policies','kind':'default'}]}}]}
        (self.root/'targets.json').write_text(json.dumps(self.targets))

    def tearDown(self):
        for p in self.patches:p.stop()
        self.tmp.cleanup()

    def get(self,url):
        if url==self.policy:
            return discovery.body_from_json(self.data),200,{'content-type':'application/json'}
        if url not in self.paths:
            raise HTTPError(url,404,'missing',{},None)
        return self.paths[url],200,{'content-type':'text/html' if url in (self.url,self.url+'one/') else 'text/plain'}

    def test_capture_detect_change_delete_legacy_history(self):
        date=datetime(2026,10,29,tzinfo=timezone.utc)
        first=monitor.run(date,self.get)
        self.assertEqual(len(first),4) # home + dataset + page + policy record
        self.assertEqual(len([r for r in first if r.get('logical')]),1)
        s=json.loads((self.root/'data/summary.json').read_text())
        self.assertEqual(s['stats']['one']['resource_count'],4)
        self.assertTrue(s['coverage']['one']['complete'])
        self.paths[self.url+'one/']=b'<h1>New policy text</h1>'
        second=monitor.run(date+timedelta(days=1),self.get)
        self.assertEqual(next(r for r in second if r['url']==self.url+'one/') ['change'],'changed')
        self.data['items']=[]
        del self.paths[self.url+'one/']
        third=monitor.run(date+timedelta(days=2),self.get)
        self.assertTrue(any(r['change']=='removed' and r['logical'] for r in third))
        self.assertTrue(any(r['status']=='gone' and r['http_status']==404 for r in third))
        self.assertEqual(json.loads((self.root/'data/history.json').read_text())[-1]['run_id'],third[-1]['run_id'])
        fourth = monitor.run(date+timedelta(days=3), self.get)
        self.assertFalse(any(r['logical'] and r['change']=='removed' for r in fourth))
        self.assertTrue(any(r['status']=='gone' and r['change']=='still_gone' for r in fourth))

if __name__=='__main__':unittest.main()
