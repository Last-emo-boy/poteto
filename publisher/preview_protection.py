"""Project-scoped preview keys and host-scoped browser access; never deployment credentials."""
import html
import json
import re
import secrets
import sqlite3
import time
from contextlib import closing
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import InvalidToken
from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from publisher.store import ARTIFACT_DRIVER, SANDBOX_DRIVER, Problem, ident
from publisher.access_contract import KEY_PATTERN

COOKIE = '__Host-poteto-preview'
KEY = KEY_PATTERN
LOGIN_PATH = '/_poteto/access'
CHECK_PATH = '/internal/preview-access/check'
SESSION_TTL = 12 * 3600
KIND = 'project-protection'


def policy(db, project_id):
    return db.execute('SELECT * FROM preview_protection WHERE project_id=?', (project_id,)).fetchone()


def supported(store, project):
    return project['preview_mode'] == 'isolated' and project['app_domain'] == store.settings.new_app_domain


def view(store, actor, project_id):
    with store.connection() as db:
        project = store.project(db, actor, project_id, 'deployments:read', allow_preview=True)
        row = policy(db, project_id)
        job = db.execute('SELECT state,error FROM jobs WHERE id=?', (row['job_id'],)).fetchone() if row else None
    return {'project_id': project_id, 'enabled': bool(row and row['enabled']),
            'available': store.settings.preview_protection and supported(store, project),
            'state': job['state'] if job else 'disabled', 'error': job['error'] if job else None,
            'job_id': row['job_id'] if row else None,
            'protected': bool(row and row['enabled'] and job and job['state'] == 'completed')}


def configure(store, actor, project_id, action):
    with store.connection() as db:
        project = store.project(db, actor, project_id, 'projects:write', allow_inactive=True)
        store.permit(actor, 'deployments:write', project_id)
        if project['state'] != 'active':
            raise Problem(409, 'Choose an active project')
        if not supported(store, project):
            raise Problem(422, 'Preview protection requires a personal project on the separate application domain')
        if action != 'disable' and not store.settings.preview_protection:
            raise Problem(503, 'Preview protection is not enabled on this platform')
        if db.execute('SELECT 1 FROM deployments WHERE project_id=? AND runtime_driver IS NOT NULL '
                      'AND runtime_driver NOT IN (?,?)', (project_id,ARTIFACT_DRIVER,SANDBOX_DRIVER)).fetchone():
            raise Problem(409, 'This project contains a runtime that does not support preview protection')
        row = policy(db, project_id)
        current = db.execute('SELECT * FROM jobs WHERE id=?', (row['job_id'],)).fetchone() if row else None
        if action == 'retry':
            if current is None or current['state'] != 'failed':
                raise Problem(409, 'Only a failed protection job can be retried')
            if db.execute("SELECT 1 FROM jobs WHERE project_id=? AND state IN ('uploading','queued','running')", (project_id,)).fetchone():
                raise Problem(409, 'Project has another active operation')
            db.execute("UPDATE jobs SET state='queued',owner=NULL,lease_until=NULL,attempts=0,error=NULL,updated=? WHERE id=?", (time.time(),current['id']))
            store.audit(db, actor, 'protection.retry', project_id)
            return {'project_id':project_id,'job_id':current['id'],'enabled':bool(row['enabled'])}
        if db.execute("SELECT 1 FROM jobs WHERE project_id=? AND state IN ('uploading','queued','running')", (project_id,)).fetchone():
            raise Problem(409, 'Finish the current project operation before changing protection')
        if db.execute("SELECT 1 FROM jobs WHERE project_id=? AND kind='project-import' AND state IN ('failed','cancelling')", (project_id,)).fetchone():
            raise Problem(409, 'Finish or discard the reserved project import first')
        enabled = bool(row and row['enabled'])
        if action == 'enable' and enabled:
            raise Problem(409, 'Protection is already enabled; rotate the key instead')
        if action == 'rotate' and (not enabled or current is None or current['state'] != 'completed'):
            raise Problem(409, 'Finish enabling protection before rotating its key')
        key = 'poteto_preview_' + secrets.token_urlsafe(32) if action != 'disable' else None
        revision, now = ident(), time.time()
        if action == 'rotate':
            db.execute('UPDATE preview_protection SET key_hash=?,revision=?,updated=? WHERE project_id=?',
                       (store.token_hash(key),revision,now,project_id))
            job_id = current['id']
        else:
            job_id = ident()
            db.execute('INSERT INTO jobs(id,project_id,kind,created,updated) VALUES(?,?,?,?,?)', (job_id,project_id,KIND,now,now))
            db.execute('INSERT INTO preview_protection(project_id,enabled,key_hash,revision,job_id,updated) VALUES(?,?,?,?,?,?) '
                       'ON CONFLICT(project_id) DO UPDATE SET enabled=excluded.enabled,key_hash=excluded.key_hash,'
                       'revision=excluded.revision,job_id=excluded.job_id,updated=excluded.updated',
                       (project_id,action!='disable',store.token_hash(key) if key else None,revision,job_id,now))
        store.audit(db, actor, 'protection.'+action, project_id)
    result = {'project_id':project_id,'job_id':job_id,'enabled':action!='disable'}
    if key:result['access_key'] = key
    return result



def decorate(backend, deployment, document):
    """Attach protection only to this immutable preview route."""
    if backend.store is None:return document
    with backend.store.connection() as db:
        row = policy(db,deployment['project_id'])
    enabled = bool(row and row['enabled'])
    key = 'publisher-preview-' + deployment['id']
    http = document['http']; router = http['routers'][key]
    names = {suffix:key+'-'+suffix for suffix in ['context','guard','clean','login','path']}
    router['middlewares'] = [value for value in router.get('middlewares',[]) if value not in names.values()]
    http['routers'].pop(names['login'],None)
    http['services'].pop(names['login'],None)
    for name in names.values():http.setdefault('middlewares',{}).pop(name,None)
    if not enabled:return document
    upstream = backend.config.get('preview_access_url','http://172.18.0.1:18000').rstrip('/')
    parsed = urlsplit(upstream)
    if parsed.scheme!='http' or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('Preview access requires an internal HTTP API origin')
    http['middlewares'][names['context']] = {'headers':{'customRequestHeaders':{'X-Poteto-Release':deployment['id']}}}
    http['middlewares'][names['guard']] = {'forwardAuth':{
        'address':upstream+CHECK_PATH,'trustForwardHeader':False,
        'authRequestHeaders':['X-Poteto-Release','Cookie','X-Poteto-Access-Key','X-Poteto-Probe','X-Forwarded-Uri','X-Forwarded-Method'],
        'authResponseHeaders':['Cookie']}}
    http['middlewares'][names['clean']] = {'headers':{
        'customRequestHeaders':{'X-Poteto-Release':'','X-Poteto-Access-Key':'','X-Poteto-Probe':''},
        'customResponseHeaders':{'Cache-Control':'private, no-store','X-Poteto-Protection':'enabled'}}}
    http['middlewares'][names['path']] = {'replacePath':{'path':'/internal/preview-access/login'}}
    router['middlewares'] = [names['context'],*router['middlewares'],names['guard'],names['clean']]
    http['routers'][names['login']] = {'rule':router['rule']+' && Path(`'+LOGIN_PATH+'`)',
        'entryPoints':router['entryPoints'],'priority':10000,'service':names['login'],
        'middlewares':[names['context'],names['path']]}
    http['services'][names['login']] = {'loadBalancer':{'servers':[{'url':upstream}]}}
    return document


def enabled(backend, project_id):
    if backend.store is None:return False
    with backend.store.connection() as db:
        row=policy(db,project_id)
    return bool(row and row['enabled'])


def probe_headers(backend, deployment, path):
    return {'X-Poteto-Probe':probe(backend.store,deployment['id'],path)} if enabled(backend,deployment['project_id']) else {}


def token(store, purpose, **fields):
    return store.cipher.encrypt(json.dumps({'purpose':purpose}|fields).encode()).decode()


def claims(store, value, purpose, ttl):
    try:
        result = json.loads(store.cipher.decrypt(value.encode(),ttl=ttl))
    except (InvalidToken, ValueError, UnicodeError, TypeError, AttributeError):
        return None
    return result if isinstance(result,dict) and result.get('purpose') == purpose else None


def probe(store, release_id, path):
    return token(store, 'preview-route-probe', release=release_id, path=path)


def lookup(store, release_id):
    if not isinstance(release_id,str) or not re.fullmatch('[a-f0-9]{24}',release_id):
        raise Problem(404, 'Preview not found')
    # Public asset requests must not take the control plane's write transaction lock.
    with closing(sqlite3.connect(store.settings.db.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
        db.row_factory=sqlite3.Row
        release = db.execute("SELECT d.id,d.project_id,p.app_domain,p.preview_mode FROM deployments d JOIN projects p ON p.id=d.project_id "
                             "JOIN accounts a ON a.id=p.account_id WHERE d.id=? AND p.state='active' AND a.enabled=1", (release_id,)).fetchone()
        if release is None:raise Problem(404, 'Preview not found')
        row = policy(db, release['project_id'])
    return release, row


def cookies(header):
    parts = [part.strip() for part in header.split(';') if part.strip()]
    value = next((p.partition('=')[2] for p in parts if p.partition('=')[0] == COOKIE), '')
    clean = '; '.join(p for p in parts if p.partition('=')[0] != COOKIE)
    return value, clean


def key_matches(store, row, value):
    return bool(KEY.fullmatch(value or '') and row and row['key_hash']
                and secrets.compare_digest(row['key_hash'], store.token_hash(value)))


def destination(value):
    return value if isinstance(value,str) and value.startswith('/') and len(value)<=2048 and not any(ord(c)<32 for c in value) else '/'


def page(next_path='/', *, wrong=False):
    message = '<p class="error" role="alert">That access key did not match. Try again.</p>' if wrong else ''
    return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' \
        '<title>Private preview · Poteto</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f6ef;color:#153a2e;font:17px/1.5 system-ui,sans-serif}' \
        'main{width:min(400px,calc(100% - 56px));padding:28px}small{font-weight:650;letter-spacing:.08em}h1{font-size:42px;line-height:1.1;letter-spacing:-.04em;margin:32px 0 12px}' \
        'p{color:#53665c}label{display:block;margin:28px 0 8px}input,button{box-sizing:border-box;width:100%;font:inherit;border-radius:9px;padding:13px 15px}' \
        'input{background:white;border:1px solid #87978b;color:#153a2e}button{margin-top:14px;background:#183f30;color:#f1f7e7;border:0;font-weight:650;cursor:pointer}' \
        'input:focus-visible,button:focus-visible{outline:3px solid #819c52;outline-offset:3px}.error{color:#a52c26}</style>' \
        '<main><small>POTETO / PREVIEW</small><h1>A private preview.</h1><p>Ask the project owner for an access key to view this version.</p>' + message + \
        '<form method="post" action="'+LOGIN_PATH+'"><input type="hidden" name="next" value="'+html.escape(destination(next_path),quote=True)+'">' \
        '<label for="access-key">Access key</label><input id="access-key" type="password" name="access_key" autocomplete="current-password" required maxlength="80">' \
        '<button type="submit">View preview</button></form></main></html>'


def challenge(next_path='/', *, wrong=False):
    return HTMLResponse(page(next_path,wrong=wrong),status_code=401,headers={
        'Cache-Control':'private, no-store','X-Poteto-Protection':'enabled','X-Frame-Options':'DENY',
        'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"})


def register(app, store, actor):
    @app.get('/v1/projects/{project_id}/protection',tags=['Projects'])
    def status(project_id: str, user=Depends(actor)):
        return view(store,user,project_id)

    @app.post('/v1/projects/{project_id}/protection',status_code=202,tags=['Projects'])
    def enable(project_id: str, user=Depends(actor)):
        return configure(store,user,project_id,'enable')

    @app.post('/v1/projects/{project_id}/protection/{action}',status_code=202,tags=['Projects'])
    def change(project_id: str, action: str, user=Depends(actor)):
        if action not in {'disable','rotate','retry'}:raise Problem(404,'Protection operation not found')
        return configure(store,user,project_id,action)

    @app.get(CHECK_PATH,include_in_schema=False)
    def check(request: Request):
        release,row = lookup(store,request.headers.get('X-Poteto-Release'))
        value,clean = cookies(request.headers.get('Cookie',''))
        if not row or not row['enabled']:
            return Response(status_code=204,headers={'Cookie':clean})
        proof = claims(store,request.headers.get('X-Poteto-Probe'), 'preview-route-probe', 90)
        method = request.headers.get('X-Forwarded-Method','GET')
        uri = request.headers.get('X-Forwarded-Uri','/')
        session = claims(store,value,'preview-session',SESSION_TTL)
        allowed = (key_matches(store,row,request.headers.get('X-Poteto-Access-Key'))
                   or (session and session.get('release')==release['id'] and session.get('revision')==row['revision'])
                   or (proof and method=='GET' and proof.get('release')==release['id'] and proof.get('path')==uri))
        return Response(status_code=204,headers={'Cookie':clean}) if allowed else challenge(uri)

    @app.api_route('/internal/preview-access/login',methods=['GET','POST'],include_in_schema=False)
    async def login(request: Request):
        release,row=lookup(store,request.headers.get('X-Poteto-Release'))
        host=store.settings.hostname(release['project_id'],release['id'],domain=release['app_domain'])
        if not row or not row['enabled']:return RedirectResponse('https://'+host+'/',status_code=303)
        if request.method=='GET':return challenge(request.headers.get('X-Forwarded-Uri','/'))
        if request.headers.get('Content-Type','').split(';')[0]!='application/x-www-form-urlencoded':
            raise Problem(415,'Submit the preview access form')
        body=bytearray()
        async for chunk in request.stream():
            if len(body)+len(chunk)>4096:raise Problem(413,'Access form is too large')
            body.extend(chunk)
        if request.headers.get('Origin') not in {None,'https://'+host}:
            raise Problem(403,'Submit the access form from this preview')
        try:fields=parse_qs(body.decode(),max_num_fields=4)
        except (ValueError,UnicodeError):raise Problem(422,'Invalid access form') from None
        next_path=destination(fields.get('next',['/'])[0])
        if len(fields.get('access_key',[]))!=1 or not key_matches(store,row,fields['access_key'][0]):
            return challenge(next_path,wrong=True)
        response=RedirectResponse('https://'+host+next_path,status_code=303)
        response.set_cookie(COOKIE,token(store,'preview-session',release=release['id'],revision=row['revision']),
                            secure=True,httponly=True,samesite='lax',max_age=SESSION_TTL,path='/')
        return response
