"""Read-only public-source evidence for an MVP review. No keys, transactions or writes to production."""
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path('evidence')
BASE = 'https://bait-production-2706.up.railway.app'
manifest = []

def get(url, name, cap=32_000_000):
    started = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Bait-MVP-verification/1.0'})
        try:
            response = urllib.request.urlopen(req, timeout=35)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read(cap + 1)
            status = response.status
        if len(body) > cap:
            raise ValueError('response exceeds evidence cap; no truncated result accepted')
        (ROOT / name).write_bytes(body)
        manifest.append({'url': url, 'file': name, 'status': status, 'bytes': len(body), 'seconds': round(time.monotonic()-started,3)})
        return body
    except Exception as error:
        manifest.append({'url':url,'file':name,'error':type(error).__name__})
        return b''

class Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]; self.tables=[]; self.table=None; self.row=None; self.cell=None
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='a' and a.get('href'): self.links.append(a['href'])
        if tag=='table': self.table=[]
        if tag=='tr' and self.table is not None: self.row=[]
        if tag in ('td','th') and self.row is not None: self.cell=[]
    def handle_data(self,data):
        if self.cell is not None:self.cell.append(data)
    def handle_endtag(self,tag):
        if tag in ('td','th') and self.cell is not None:
            self.row.append(''.join(self.cell)); self.cell=None
        if tag=='tr' and self.row is not None:
            self.table.append(self.row); self.row=None
        if tag=='table' and self.table is not None:
            self.tables.append(self.table); self.table=None

hooklist=get('https://raw.githubusercontent.com/Uniswap/hooklist/main/hooklist.json','hooklist-complete.json')
if hooklist:
    try:
        registry=json.loads(hooklist)
        print('REGISTRY_SHAPE',str(type(registry)), list(registry)[:10] if isinstance(registry,dict) else len(registry))
    except ValueError:
        print('REGISTRY_INVALID')
for slug in ['bunni','aegis','aegis-v3-hook-unichain']:
    get(BASE+'/p/'+slug, 'extra-'+slug+'.html')
networks={'ethereum':'eth','robinhood':'robinhood','base':'base','bnb':'bsc','arbitrum':'arbitrum','unichain':'unichain','ink':'ink'}
comparisons=[]
for slug in ['stablepairhook','angstrom','hookrmodularhookv6','fablesrampeth','claus']:
    file=ROOT/('project-'+slug+'.html')
    if not file.exists():continue
    parsed=Links(); parsed.feed(file.read_text())
    refs=[]
    for url in parsed.links:
        m=re.fullmatch(r'https://app\.uniswap\.org/explore/pools/([^/]+)/(0x[a-fA-F0-9]{64})',url)
        if m and (m[1],m[2]) not in refs:refs.append((m[1],m[2]))
    record={'slug':slug,'tables':parsed.tables,'pools':[]}
    for chain,pool in refs[:2]:
        network=networks.get(chain)
        if not network:continue
        url=f'https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool}'
        body=get(url, f'gt-{slug}-{pool[-8:]}.json');time.sleep(7)
        candles=get(url+'/ohlcv/day?aggregate=1&limit=20&currency=usd',f'gt-candles-{slug}-{pool[-8:]}.json');time.sleep(7)
        r={'chain':chain,'pool':pool,'url':url}
        try:
            d=json.loads(body);r['pool_data']=d.get('data',{}).get('attributes',{})
            c=json.loads(candles);r['candles']=c.get('data',{}).get('attributes',{}).get('ohlcv_list',[])
        except ValueError:r['parse_error']=True
        record['pools'].append(r)
    comparisons.append(record)
(ROOT/'pool-comparisons.json').write_text(json.dumps(comparisons,indent=2))
(ROOT/'source-manifest.json').write_text(json.dumps({'observed_at':datetime.now(timezone.utc).isoformat(),'requests':manifest},indent=2))
print('SOURCE_CHECK',json.dumps(manifest))
