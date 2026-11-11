import http from 'node:http';

const server = http.createServer(async (request, response) => {
  const json = (status, value) => { response.writeHead(status, {'content-type': 'application/json'}); response.end(JSON.stringify(value)); };
  if (request.url === '/healthz') return json(200, {status: 'ok'});
  if (request.url === '/api/state') return json(200, {version: process.env.APP_VERSION || 'node-v1', secret_configured: Boolean(process.env.TEST_SECRET), host: request.headers.host, protocol: request.headers['x-forwarded-proto'] || 'http'});
  if (request.method === 'POST' && request.url === '/api/echo') {
    const chunks = []; let size = 0;
    for await (const chunk of request) {
      size += chunk.length;
      if (size > 1024 * 1024) return json(413, {error: 'body too large'});
      chunks.push(chunk);
    }
    try { return json(200, {method: request.method, body: JSON.parse(Buffer.concat(chunks).toString('utf8'))}); }
    catch { return json(400, {error: 'invalid JSON'}); }
  }
  if (request.url === '/') {
    response.writeHead(200, {'content-type': 'text/html; charset=utf-8'});
    return response.end('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>PaaS · Node</title><h1>Node 应用已发布</h1><p>源码在隔离构建 VM 中构建，应用运行在独立租户 VM。</p></html>');
  }
  json(404, {error: 'not found'});
});
server.listen(Number(process.env.PORT || 3000), '0.0.0.0');
