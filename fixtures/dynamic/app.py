import json
import os
import sqlite3
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOOT_ID = str(uuid.uuid4())
STARTED = time.time()
DATABASE = os.environ.get("DATABASE_PATH", "/data/site.db")


def connect():
    return sqlite3.connect(DATABASE, timeout=5)


with connect() as db:
    db.execute("CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, value, content_type="application/json"):
        body = json.dumps(value, ensure_ascii=False).encode() if content_type == "application/json" else value.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            return self.respond(200, {"status": "ok"})
        if self.path == "/api/state":
            with connect() as db:
                records = [{"id": row[0], "value": row[1]} for row in db.execute("SELECT id,value FROM records ORDER BY id")]
            return self.respond(200, {"boot_id": BOOT_ID, "uptime_seconds": round(time.time() - STARTED), "records": records})
        if self.path == "/":
            return self.respond(200, PAGE, "text/html")
        self.respond(404, {"error": "Not found"})

    def do_POST(self):
        if self.path not in ("/api/echo", "/api/records"):
            return self.respond(404, {"error": "Not found"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.respond(400, {"error": "Invalid content length"})
        if not 0 < size <= 65536:
            return self.respond(413, {"error": "Body must be 1–65536 bytes"})
        try:
            payload = json.loads(self.rfile.read(size))
        except (ValueError, UnicodeDecodeError):
            return self.respond(400, {"error": "Expected JSON"})
        if self.path == "/api/echo":
            return self.respond(200, {"boot_id": BOOT_ID, "method": "POST", "body": payload})
        token = os.environ.get("WRITE_TOKEN")
        if not token or self.headers.get("Authorization") != "Bearer " + token:
            return self.respond(401, {"error": "Write token required"})
        if not isinstance(payload, dict) or not isinstance(payload.get("value"), str):
            return self.respond(400, {"error": "value must be a string"})
        with connect() as db:
            result = db.execute("INSERT INTO records(value) VALUES (?)", (payload["value"],))
        self.respond(201, {"id": result.lastrowid, "value": payload["value"]})


PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OVH PaaS · 动态站点</title><style>body{margin:0;background:#111720;color:#e8edf5;font:17px/1.7 system-ui,sans-serif}main{max-width:760px;margin:12vh auto;padding:32px}small{color:#93a8c4;letter-spacing:.14em}h1{font-size:clamp(38px,7vw,60px);line-height:1.15;letter-spacing:-.04em}p{color:#aebdd1}pre{padding:24px;background:#1a2534;border-radius:16px;white-space:pre-wrap;word-break:break-word;font-size:14px}button{background:#bbd4ff;color:#152239;border:0;border-radius:10px;padding:13px 20px;font:inherit;cursor:pointer}footer{margin-top:48px;font-size:13px;color:#8297b5}</style><main><small>OVH PAAS / DYNAMIC</small><h1>闲置休眠。<br>访问时唤醒。</h1><p>这是一项真实运行的 Python 服务。启动标识会在容器重新启动后变化，保存的数据则留在独立数据卷中。</p><button onclick="refresh()">刷新运行状态</button><pre id="state">正在读取运行状态…</pre><footer>Dokploy + Sablier · 数据只读展示，写入需要独立凭据</footer></main><script>async function refresh(){const el=document.getElementById('state');try{const r=await fetch('/api/state');if(!r.ok)throw Error('HTTP '+r.status);el.textContent=JSON.stringify(await r.json(),null,2)}catch(e){el.textContent='读取失败：'+e.message}}refresh()</script></html>"""

if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
