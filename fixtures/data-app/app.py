from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import uuid
from database import query,literal

BOOT=uuid.uuid4().hex
DATA=Path(os.environ['PAAS_DATA_DIR'])


class Handler(BaseHTTPRequestHandler):
    def respond(self,status,value):
        content=json.dumps(value,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)

    def do_GET(self):
        try:
            if self.path=='/healthz':
                query('SELECT 1;');return self.respond(200,{'status':'ok'})
            if self.path=='/api/state':
                rows=json.loads(query("SELECT coalesce(json_agg(r),'[]') FROM (SELECT id,value FROM records ORDER BY id) r;"))
                admin=query("SELECT rolsuper FROM pg_roles WHERE rolname=current_user;")=='t'
                files={p.name:p.read_text() for p in DATA.iterdir() if p.is_file()}
                return self.respond(200,{'records':rows,'files':files,'database_superuser':admin,'boot_id':BOOT,'version':os.environ.get('APP_VERSION','data-v1')})
            if self.path=='/':
                content=(Path(__file__).parent/'index.html').read_bytes()
                self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8')
                self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content);return
            self.respond(404,{'error':'not found'})
        except Exception:
            self.respond(503,{'error':'Application data is not ready'})

    def do_POST(self):
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=1024*1024:
                return self.respond(413,{'error':'Body must be at most 1 MiB'})
            body=json.loads(self.rfile.read(size))
            if self.path=='/api/records':
                row=json.loads(query('INSERT INTO records(value) VALUES('+literal(body['value'])+') RETURNING row_to_json(records);'))
                return self.respond(201,row)
            if self.path=='/api/uploads':
                name=body['name'];text=body['text']
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',name) or not isinstance(text,str):
                    raise ValueError('Invalid file name or text')
                path=DATA/name
                if path.is_symlink():
                    raise ValueError('Symlinks are not writable')
                path.write_text(text)
                return self.respond(201,{'name':name,'bytes':len(text.encode())})
            self.respond(404,{'error':'not found'})
        except (ValueError,KeyError):
            self.respond(400,{'error':'Invalid input'})
        except Exception:
            self.respond(503,{'error':'Application data operation failed'})


ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','3000'))),Handler).serve_forever()
