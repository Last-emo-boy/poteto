"""Authenticated private client for the same-host execution supervisor."""
import base64
import http.client
import json
from pathlib import Path
import time
import urllib.parse
from publisher.controlflow import ObservationUnavailable


class ExecutionFailure(RuntimeError):
    pass


class BuildOutput:
    """Read complete UTF-8 lines from a bounded byte log without replaying chunks."""

    def __init__(self, client, build_id, emit):
        self.client, self.build_id, self.emit = client, build_id, emit
        self.offset = self.lines = 0
        self.pending = b''

    def line(self, data):
        if self.lines >= 4000:
            if self.lines == 4000:
                self.emit('Build output limited to 4000 lines')
                self.lines += 1
            return
        self.emit(data.rstrip(b'\r').decode('utf-8', errors='replace'))
        self.lines += 1

    def read(self, *, final=False):
        while self.lines <= 4000:
            try:
                chunk = self.client.build_log_chunk(self.build_id, self.offset)
            except ObservationUnavailable:
                if final:
                    raise
                return  # A progress read can wait for the next observation of the same job.
            if not chunk:
                break
            self.offset += len(chunk)
            *lines, self.pending = (self.pending + chunk).split(b'\n')
            for line in lines:
                self.line(line)
                if self.lines > 4000:
                    break
            if not final:
                return
        if final and self.pending:
            self.line(self.pending)
            self.pending = b''


class ExecutionClient:
    def __init__(self, config):
        parsed = urllib.parse.urlparse(config["url"])
        if parsed.scheme != "http" or parsed.hostname != "10.201.0.2" or parsed.port != 21999:
            raise ValueError("Execution supervisor must use its private host-only endpoint")
        self.config = config

    def request(self, method, path, body=None, headers=None, *, timeout=240):
        connection = http.client.HTTPConnection("10.201.0.2", 21999, timeout=timeout)
        request_headers = {"Authorization": "Bearer " + self.config["token"], "Content-Type": "application/json"} | (headers or {})
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            data = response.read(3 * 1024**2)
            value = json.loads(data)
            if response.status >= 500:
                raise ObservationUnavailable('Execution supervisor is temporarily unavailable; existing operation will be reconciled')
            if response.status >= 400:
                error = ExecutionFailure(value.get("error", "Execution request failed"))
                error.status = response.status
                raise error
            return value
        except (OSError, http.client.HTTPException, ValueError):
            raise ObservationUnavailable('Execution observation was interrupted; existing operation will be reconciled') from None
        finally:
            connection.close()

    def start_build(self, build_id, spec, archive):
        with Path(archive).open("rb") as source:
            return self.request("POST", "/v1/builds/" + build_id, source,
                                {"Content-Length": str(Path(archive).stat().st_size),
                                 "Content-Type": "application/x-tar" if spec.get('runtime')=='image-import' else "application/gzip",
                                 "X-Paas-Build-Spec": base64.urlsafe_b64encode(json.dumps(spec).encode()).decode()})

    def build_status(self, build_id):
        return self.request("GET", "/v1/builds/" + build_id)

    def ensure_runtime(self, account_id, public_key):
        return self.request("POST", "/v1/runtimes/" + account_id, {"deploy_public_key": public_key})

    def runtime_status(self, account_id):
        return self.request('GET','/v1/runtimes/'+account_id+'/status')

    def retire_runtime(self, account_id, *, commit=False):
        action='commit-retirement' if commit else 'prepare-retirement'
        return self.request('POST',f'/v1/runtimes/{account_id}/{action}',timeout=360)

    def build_logs(self, build_id):
        return self.request("GET", "/v1/builds/" + build_id + "/logs")["log"]

    def build_log_chunk(self, build_id, offset):
        result = self.request('GET', f'/v1/builds/{build_id}/logs/{offset}', timeout=10)
        return base64.b64decode(result['data'], validate=True)

    def build_operation(self, account_id, build_id, action):
        if action not in {'cancel', 'delete'}:
            raise ValueError('Unsupported build operation')
        return self.request('POST', '/v1/builds/' + build_id + '/' + action, {'account_id': account_id})

    def load_image(self, account_id, build_id):
        return self.request("POST", "/v1/runtimes/" + account_id + "/images/" + build_id)

    def bootstrap(self, account_id):
        return self.request('POST', '/v1/runtimes/' + account_id + '/bootstrap')

    def action(self, account_id, release_id, action, **fields):
        return self.request('POST', '/v1/runtimes/' + account_id + '/releases/' + release_id, {'action': action} | fields)

    def sandbox(self, release_id, account_id, project_id, action, **fields):
        return self.request('POST', '/v1/sandboxes/' + release_id,
                            fields | {'action': action, 'account_id': account_id, 'project_id': project_id})

    def data(self, account_id, resource_id, project_id, action, **fields):
        return self.request('POST','/v1/runtimes/'+account_id+'/data',
            {'action':action,'resource_id':resource_id,'project_id':project_id} | fields)

    def native_database(self, account_id, resource_id, project_id, action, **fields):
        return self.request('POST', '/v1/databases/' + resource_id,
                            fields | {'action':action,'account_id':account_id,'project_id':project_id})

    def restore_data(self,account_id,spec,payload):
        with Path(payload).open('rb') as source:
            return self.request('POST','/v1/runtimes/'+account_id+'/restore-data',source,{
                'Content-Type':'application/octet-stream','Content-Length':str(Path(payload).stat().st_size),
                'X-Paas-Restore-Spec':base64.urlsafe_b64encode(json.dumps(spec).encode()).decode()},timeout=960)

    def restore_native_database(self,account_id,spec,payload):
        with Path(payload).open('rb') as source:
            return self.request('POST','/v1/databases/'+spec['resource_id']+'/restore',source,{
                'Content-Type':'application/octet-stream','Content-Length':str(Path(payload).stat().st_size),
                'X-Paas-Restore-Spec':base64.urlsafe_b64encode(json.dumps(spec|{'account_id':account_id}).encode()).decode()},timeout=960)

    def stream_export(self,method,path,output,limit,check,*,body=None,headers=None):
        connection=http.client.HTTPConnection('10.201.0.2',21999,timeout=440)
        request_headers={'Authorization':'Bearer '+self.config['token'],'Content-Type':'application/json'}|(headers or {})
        try:
            connection.request(method,path,body=json.dumps(body).encode() if body is not None else None,headers=request_headers)
            response=connection.getresponse()
            if response.status>=500:raise ObservationUnavailable('Export executor temporarily unavailable')
            if response.status!=200:raise ExecutionFailure('Owned export input is unavailable')
            length=response.length
            total=0
            while chunk:=response.read(1024**2):
                check();total+=len(chunk)
                if total>limit:raise ExecutionFailure('Export input exceeded its size limit')
                output.write(chunk)
            if not total or (length is not None and total!=length):
                raise ObservationUnavailable('Export stream did not finish')
            return total
        except (OSError,http.client.HTTPException):
            raise ObservationUnavailable('Export stream was interrupted; no partial bundle was published') from None
        finally:connection.close()
