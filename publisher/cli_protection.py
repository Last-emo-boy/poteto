"""Private preview setup and bounded, same-origin reads for deployment agents."""
import json
import os
import re
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

if __package__:
    from .access_contract import KEY_PATTERN
    from .cli_project import deployment_reference
else:
    from access_contract import KEY_PATTERN
    from cli_project import deployment_reference


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def configure(client, args):
    base = '/v1/projects/'+args.project+'/protection'
    action = args.protection_action
    if action == 'status':return client.request('GET',base)
    if action in {'enable','rotate'}:
        args.output.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        with open(args.output,'x',opener=lambda p,f:os.open(p,f,0o600)) as out:
            try:
                result=client.request('POST',base+('/rotate' if action=='rotate' else ''))
                key=result.pop('access_key')
                if not KEY_PATTERN.fullmatch(key):raise ValueError('The API returned an invalid preview access key')
                json.dump({'access_key':key,'project_id':args.project,'api':client.url},out);out.write('\n')
            except BaseException:
                args.output.unlink(missing_ok=True)
                raise
        result['output']=str(args.output)
    else:
        result=client.request('POST',base+'/'+action)
    if not args.no_wait:
        client.wait_job(result['job_id'],args.timeout)
        result.update(client.request('GET',base))
    return result


def fetch(client, reference, path, key_file=None):
    kind, release_id=deployment_reference(reference)
    if kind!='deployment' or not re.fullmatch('[a-f0-9]{24}',release_id):
        raise ValueError('Choose a preview URL or deployment ID, not a production application alias')
    release=client.request('GET','/v1/deployments/'+release_id)
    origin=urlsplit(release.get('url') or '')
    if origin.scheme!='https' or not origin.netloc or origin.username or origin.password or origin.path not in {'','/'} or origin.query or origin.fragment:
        raise ValueError('The preview must be ready at a valid HTTPS URL')
    if '://' in reference:
        requested=urlsplit(reference)
        if (requested.scheme,requested.netloc)!=(origin.scheme,origin.netloc) or requested.path not in {'','/'} or requested.query or requested.fragment:
            raise ValueError('The preview URL must match the selected API deployment; use --path for a route')
    if not path.startswith('/') or path.startswith('//') or len(path)>2048 or any(ord(c)<32 for c in path):
        raise ValueError('Use an absolute application path, not a URL')
    key=None
    if key_file:
        stored=json.loads(key_file.read_text(encoding='utf-8-sig'))
        key=stored.get('access_key')
        if stored.get('api')!=client.url or stored.get('project_id')!=release['project_id'] or not isinstance(key,str) or not KEY_PATTERN.fullmatch(key):
            raise ValueError('The access key file belongs to another API or project, or is invalid')
    opener=build_opener(NoRedirect())
    url=origin.scheme+'://'+origin.netloc+path
    for _ in range(4):
        headers={'User-Agent':'Poteto-Preview-Check','Accept':'text/html,application/json,text/plain;q=0.9'}
        if key:headers['X-Poteto-Access-Key']=key
        try:
            with opener.open(Request(url,headers=headers),timeout=30) as response:
                body=response.read(1024**2+1)
                if len(body)>1024**2:raise ValueError('Preview response exceeds 1 MiB; choose a smaller verification route')
                text=body.decode('utf-8',errors='replace')
                if key:text=text.replace(key,'[REDACTED]')
                return {'url':url,'status':response.status,'content_type':response.headers.get('Content-Type'),'body':text}
        except HTTPError as error:
            status=error.code;location=error.headers.get('Location');error.close()
            if status not in {301,302,303,307,308} or not location:
                raise RuntimeError(f'Preview returned HTTP {status}') from None
            target=urljoin(url,location);parsed=urlsplit(target)
            if (parsed.scheme,parsed.netloc)!=(origin.scheme,origin.netloc) or parsed.username or parsed.password:
                raise ValueError('Preview redirected outside its origin; the access key was not forwarded') from None
            url=target
    raise RuntimeError('Preview exceeded three same-origin redirects')
