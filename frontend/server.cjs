const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { pipeline } = require('node:stream');

const root = __dirname;
const backend = process.env.LEDGERROOM_BACKEND_URL || 'http://127.0.0.1:8010';
const mime = {'.html':'text/html; charset=utf-8','.css':'text/css; charset=utf-8','.js':'text/javascript; charset=utf-8','.svg':'image/svg+xml','.png':'image/png'};
const server = http.createServer(async (req, res) => {
  if (req.url.startsWith('/api/') || req.url === '/health') {
    try {
      const headers = {};
      for (const key of ['content-type','authorization','accept']) if (req.headers[key]) headers[key] = req.headers[key];
      let body;
      if (!['GET','HEAD'].includes(req.method)) {
        const chunks = [];
        for await (const chunk of req) chunks.push(chunk);
        body = Buffer.concat(chunks);
      }
      const upstream = await fetch(new URL(req.url, backend), {method:req.method,headers,body});
      res.writeHead(upstream.status, Object.fromEntries(upstream.headers));
      if (!upstream.body) return res.end();
      return pipeline(require('node:stream').Readable.fromWeb(upstream.body), res, err => { if(err) res.destroy(err); });
    } catch {
      res.writeHead(502, {'Content-Type':'application/json'});
      return res.end(JSON.stringify({detail:'LedgerRoom API is unavailable. Start the backend at '+backend+'.'}));
    }
  }
  let pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
  if (pathname === '/' || pathname === '/verify-email' || pathname === '/accountant-invitations/accept') pathname = '/index.html';
  const file = path.resolve(root, '.'+pathname);
  if (!file.startsWith(root + path.sep)) { res.writeHead(403); return res.end(); }
  fs.readFile(file, (err, data) => {
    if (err) { res.writeHead(404); return res.end('Not found'); }
    res.writeHead(200, {'Content-Type':mime[path.extname(file)] || 'application/octet-stream','Cache-Control':'no-store'});
    res.end(data);
  });
});
const port = Number(process.env.PORT || 5173);
server.listen(port, '0.0.0.0', () => console.log(`LedgerRoom frontend ready at http://127.0.0.1:${port} (API proxy: ${backend})`));
