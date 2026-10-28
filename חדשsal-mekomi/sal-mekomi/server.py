#!/usr/bin/env python3
"""Local-only price-file downloader. Python 3.10+, standard library only."""
import argparse
import concurrent.futures
import datetime as dt
import ftplib
import html
import http.cookiejar
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import re
import ssl
import threading
import urllib.error
import urllib.parse as up
import urllib.request as ur
import webbrowser

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / 'downloads'
MAX_BYTES = 80 * 1024 * 1024
LOCK = threading.Lock()
CHAIN_IDS = {'SHUFERSAL':'7290027600007','RAMI_LEVY':'7290058140886',
             'OSHER_AD':'7290103152017','SHEFA_BARCART_ASHEM':'7290058134977',
             'NETIV_HASED':'7290058160839','SHUK_AHIR':'7290058148776','CARREFOUR':'7290055700007'}
NAMES = {'SHUFERSAL':'שופרסל / יש חסד','RAMI_LEVY':'רמי לוי','OSHER_AD':'אושר עד',
         'SHEFA_BARCART_ASHEM':'שפע ברכת השם','NETIV_HASED':'נתיב החסד / ברכל','SHUK_AHIR':'שוק העיר','CARREFOUR':'קרפור'}


def safe_remote(url):
    p = up.urlsplit(url)
    host = (p.hostname or '').lower()
    if p.scheme not in ('https','http') or p.username or p.password:
        raise ValueError('כתובת מקור לא תקינה')
    allowed = ('prices.shufersal.co.il','binaprojects.com','app.netiv-hesed.com',
               'blob.core.windows.net','publishedprices.co.il','prices.carrefour.co.il')
    if not any(host == d or host.endswith('.'+d) for d in allowed):
        raise ValueError('שרת המקור אינו ברשימת המקורות המותרים: '+host)
    return url


class CheckedRedirect(ur.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_remote(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url):
    safe_remote(url)
    opener = ur.build_opener(CheckedRedirect)
    req = ur.Request(url, headers={'User-Agent':'SalMekomi/3.0 (local price transparency reader)'})
    with opener.open(req, timeout=35) as r:
        data = r.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError('קובץ גדול מ־80MB')
        return data


class Links(HTMLParser):
    def __init__(self, text, base):
        super().__init__(); self.links=[]; self.base=base; self.feed(text)
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag == 'a' and a.get('href'):
            self.links.append(up.urljoin(self.base, a['href']))


def file_name(link):
    parsed=up.urlsplit(link)
    params = up.parse_qs(parsed.query)
    name = params.get('FileNm',params.get('fileName',[up.unquote(parsed.path).split('/')[-1]]))[0]
    return name


def pick(entries, mode, stores):
    """One latest complete snapshot per requested branch (and subchain)."""
    candidates={}
    for name, link in entries:
        name = up.unquote(name)
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
            continue
        if mode == 'stores':
            if not re.match(r'^Stores?(?:Full)?\d', name, re.I): continue
            key = name.split('-')[0].lower()
        else:
            prefix = 'PromoFull' if mode == 'promotions' else 'PriceFull'
            m=re.match(r'^'+prefix+r'(\d{13})-(?:(\d+)-)?(\d+)-(\d{8})-?(\d{4,6})(?:\D|$)', name, re.I)
            if not m: continue
            if str(int(m[3])) not in stores: continue
            key = (m[1],m[2] or '0',str(int(m[3])))
        if key not in candidates or name > candidates[key][0]: candidates[key]=(name,link)
    return list(candidates.values())


def html_entries(url):
    text=fetch(url).decode('utf-8-sig',errors='replace')
    links=Links(text,url).links
    return [(file_name(link),link) for link in links],links


def shufersal(mode, stores):
    entries=[]
    for sid in ([None] if mode=='stores' else stores):
        params={'catID':{'stores':'5','prices':'2','promotions':'4'}[mode], '_':str(int(dt.datetime.now().timestamp()))}
        if sid: params['storeId']=sid
        root='https://prices.shufersal.co.il/FileObject/UpdateCategory?'+up.urlencode(params)
        page,links=html_entries(root); entries.extend(page)
        pages=[int(m[1]) for l in links if (m:=re.search(r'[?&]page=(\d+)',l))]
        # Listing can retain many historical files. A hard bound avoids runaway crawling.
        for page_no in range(2,min(max(pages,default=1),12)+1):
            more,_=html_entries(root+'&page='+str(page_no));entries.extend(more)
    chosen=pick(entries,mode,stores)
    return [(n,lambda u=u:fetch(u),u) for n,u in chosen]


def bina(mode, stores, chain='SHEFA_BARCART_ASHEM'):
    prefix='shuk-hayir' if chain=='SHUK_AHIR' else 'shefabirkathashem'
    base='https://'+prefix+'.binaprojects.com/'
    entries=[]
    for sid in ([None] if mode=='stores' else stores):
        query={'_':CHAIN_IDS[chain],'wReshet':'הכל',
               'WFileType':{'stores':1,'prices':4,'promotions':5}[mode],'WDate':'','WStore':sid or ''}
        records=json.loads(fetch(base+'MainIO_Hok.aspx?'+up.urlencode(query)))
        for r in records:
            n=r.get('FileNm','');entries.append((n,base+'Download.aspx?FileNm='+up.quote(n)))
    def load(url):
        records=json.loads(fetch(url)); target=records[0]['SPath']
        return fetch(up.urljoin(base,target))
    return [(n,lambda u=u:load(u),u) for n,u in pick(entries,mode,stores)]


def netiv(mode, stores):
    entries=[]
    today=dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).date()
    for offset in range(3):
        day=today-dt.timedelta(days=offset)
        for sid in ([None] if mode=='stores' else stores):
            params={'Date':day.isoformat()}
            if sid:params['StoreNumber']=sid
            root='https://app.netiv-hesed.com/?'+up.urlencode(params)
            page,_=html_entries(root);entries.extend(page)
    return [(n,lambda u=u:fetch(u),u) for n,u in pick(entries,mode,stores)]


def ftp_open(chain):
    ftp=ftplib.FTP_TLS(context=ssl.create_default_context(),timeout=35)
    ftp.connect('url.retail.publishedprices.co.il',21)
    ftp.login('RamiLevi' if chain=='RAMI_LEVY' else 'osherad','')
    ftp.prot_p(); ftp.cwd('/')
    return ftp


def cerberus_ftp(chain,mode,stores):
    with ftp_open(chain) as ftp:
        names=ftp.nlst({'stores':'*store*','prices':'*pricef*','promotions':'*promof*'}[mode])
    entries=[(n.rsplit('/',1)[-1],n) for n in names]
    def load(remote):
        out=io.BytesIO()
        def chunk(b):
            if out.tell()+len(b)>MAX_BYTES:raise ValueError('קובץ גדול מדי')
            out.write(b)
        with ftp_open(chain) as ftp:ftp.retrbinary('RETR '+remote,chunk)
        return out.getvalue()
    return [(n,lambda remote=r:load(remote),'ftps://url.retail.publishedprices.co.il/'+n)
            for n,r in pick(entries,mode,stores)]


def cerberus(chain, mode, stores):
    """Public price-publication login; cookies and CSRF stay in memory."""
    base = 'https://url.publishedprices.co.il'
    opener = ur.build_opener(CheckedRedirect, ur.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    lock = threading.Lock()
    def request(path, data=None):
        req = ur.Request(base+path, data=up.urlencode(data).encode() if data is not None else None,
                         headers={'User-Agent':'SalMekomi/3.0'})
        with lock, opener.open(req, timeout=35) as r:
            content=r.read(MAX_BYTES+1)
        if len(content)>MAX_BYTES: raise ValueError('קובץ גדול מדי')
        return content
    def token(content):
        m=re.search(r'<meta name="csrftoken" content="([^"]+)"',content.decode('utf-8',errors='replace'))
        if not m: raise ValueError('פורטל הפרסום לא החזיר הרשאת קריאה')
        return m[1]
    try:
        csrf=token(request('/login'))
        csrf=token(request('/login/user',{'r':'','username':'RamiLevi' if chain=='RAMI_LEVY' else 'osherad',
                                        'password':'','Submit':'Sign in','csrftoken':csrf}))
        rows=json.loads(request('/file/json/dir',{'iDisplayLength':10000,'sSearch':{'stores':'Stores','prices':'PriceFull','promotions':'PromoFull'}[mode],
                                                'cd':'/','csrftoken':csrf})).get('aaData',[])
        entries=[(r.get('fname',''),r.get('fname','')) for r in rows]
        return [(n,lambda n=n:request('/file/d/'+up.quote(n)),base+'/file/d/'+up.quote(n))
                for n,_ in pick(entries,mode,stores)]
    except (OSError, ValueError, KeyError):
        return cerberus_ftp(chain,mode,stores)


def carrefour(mode, stores):
    base='https://prices.carrefour.co.il/'
    page=fetch(base).decode('utf-8-sig')
    path=re.search(r"const path\s*=\s*'(\d{8})'",page)
    files=re.search(r'const files\s*=\s*(\[.*?\])',page,re.S)
    if not path or not files: raise ValueError('מבנה מקור קרפור השתנה; אפשר לייבא ידנית')
    entries=[(r['name'],base+path[1]+'/'+up.quote(r['name'])) for r in json.loads(files[1])]
    return [(n,lambda u=u:fetch(u),u) for n,u in pick(entries,mode,stores)]


def sync(chain,mode,stores):
    if chain not in CHAIN_IDS or mode not in ('stores','prices','promotions'):
        raise ValueError('רשת או פעולה לא תקינה')
    if not isinstance(stores,list) or len(stores)>20 or any(not re.fullmatch(r'\d{1,6}',str(s)) for s in stores):
        raise ValueError('בחרו עד 20 סניפים במספרים בלבד')
    stores=list(dict.fromkeys(str(int(s)) for s in stores))
    if mode!='stores' and not stores:raise ValueError('לא נבחרו סניפים')
    if chain=='SHUFERSAL': jobs=shufersal(mode,stores)
    elif chain in ('SHEFA_BARCART_ASHEM','SHUK_AHIR'):jobs=bina(mode,stores,chain)
    elif chain=='NETIV_HASED':jobs=netiv(mode,stores)
    elif chain=='CARREFOUR':jobs=carrefour(mode,stores)
    else:jobs=cerberus(chain,mode,stores)
    if not jobs:raise ValueError('לא נמצאו קובצי מקור תואמים. ייתכן שמקור הרשת השתנה או שקובץ הסניף אינו זמין. אפשר להוריד ידנית.')
    CACHE.mkdir(exist_ok=True)
    files=[];errors=[];metadata=[]
    def download(job):
        name,loader,source=job
        content=loader()
        if not content or content.lstrip().lower().startswith((b'<!doctype html',b'<html')):
            raise ValueError('המקור החזיר דף אינטרנט במקום קובץ')
        dest=CACHE/name
        temp=dest.with_suffix(dest.suffix+'.part');temp.write_bytes(content);temp.replace(dest)
        return name,source
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        future_map={pool.submit(download,j):j[0] for j in jobs}
        for future in concurrent.futures.as_completed(future_map):
            try:
                name,source=future.result();files.append(name);metadata.append({'file':name,'source':source,'downloaded':dt.datetime.now(dt.timezone.utc).isoformat()})
            except Exception as e:errors.append(future_map[future]+': '+str(e))
    if metadata:(CACHE/'last-sync.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    if not files:raise ValueError('ההורדה נכשלה: '+'; '.join(errors))
    # A partial response must never imply all requested branches were refreshed.
    found=set()
    for n in files:
        prefix='PromoFull' if mode=='promotions' else 'PriceFull'
        m=re.match(r'^'+prefix+r'\d{13}-(?:\d+-)?(\d+)-\d{8}',n,re.I)
        if m:found.add(str(int(m[1])))
    if mode!='stores':
        missing=set(stores)-found
        if missing:errors.append('לא נמצאו מחירונים לסניפים: '+', '.join(sorted(missing)))
    return {'files':sorted(files),'warning':'\n'.join(errors)}


class Handler(BaseHTTPRequestHandler):
    def good_host(self):
        return self.headers.get('Host','') in {f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
    def reply(self,code,data,kind='application/json; charset=utf-8'):
        b=json.dumps(data,ensure_ascii=False).encode() if isinstance(data,dict) else data
        self.send_response(code);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(b)))
        self.send_header('X-Content-Type-Options','nosniff');self.send_header('Cache-Control','no-store');self.send_header('Referrer-Policy','same-origin');self.end_headers();self.wfile.write(b)
    def do_GET(self):
        if not self.good_host():return self.reply(403,{'error':'Host rejected'})
        path=up.unquote(up.urlsplit(self.path).path)
        if path in ('/','/index.html'):return self.reply(200,(ROOT/'index.html').read_bytes(),'text/html; charset=utf-8')
        if path=='/api/status':return self.reply(200,{'ok':True,'version':2})
        if path.startswith('/downloads/'):
            name=path[len('/downloads/'):]
            if not re.fullmatch(r'[A-Za-z0-9_.-]+',name):return self.reply(400,{'error':'שם קובץ לא תקין'})
            target=CACHE/name
            if target.is_file():return self.reply(200,target.read_bytes(),'application/octet-stream')
        return self.reply(404,{'error':'לא נמצא'})
    def do_POST(self):
        if not self.good_host():return self.reply(403,{'error':'Host rejected'})
        origin=self.headers.get('Origin')
        if origin not in (f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'):
            return self.reply(403,{'error':'בקשה ממקור אחר נדחתה'})
        if self.path!='/api/sync':return self.reply(404,{'error':'לא נמצא'})
        if not self.headers.get('Content-Type','').startswith('application/json'):return self.reply(400,{'error':'JSON required'})
        if not LOCK.acquire(blocking=False):return self.reply(409,{'error':'עדכון אחר כבר פועל'})
        try:
            length=int(self.headers.get('Content-Length',0))
            if length<1 or length>8192:raise ValueError('בקשה גדולה מדי או ריקה')
            data=json.loads(self.rfile.read(length));result=sync(data.get('chain'),data.get('mode'),data.get('stores',[]));self.reply(200,result)
        except Exception as e:self.reply(400,{'error':str(e)[:1800]})
        finally:LOCK.release()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8765);parser.add_argument('--no-browser',action='store_true');args=parser.parse_args()
    try:server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    except OSError:
        print('Port is occupied. Close another instance, or run: python server.py --port 8766');return
    url=f'http://127.0.0.1:{args.port}/'
    print('Sal Mekomi is running locally: '+url+'\nKeep this window open. Ctrl+C to stop.')
    if not args.no_browser:threading.Timer(.5,lambda:webbrowser.open(url)).start()
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__=='__main__':main()
