import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { createDeploymentService, ServiceError } from './github.js';
import { archiveLimits, inspectArchive, validateFiles } from './archive.js';
import { fetchPublicGithubSource } from './public-github.js';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', 'public');
const assets = new Map([
  ['/', ['index.html', 'text/html; charset=utf-8']],
  ['/app.js', ['app.js', 'text/javascript; charset=utf-8']],
  ['/styles.css', ['styles.css', 'text/css; charset=utf-8']],
]);

function json(response, code, data) {
  response.writeHead(code, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', 'x-content-type-options': 'nosniff' });
  response.end(JSON.stringify(data));
}

async function readLimited(request, limit) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > limit) throw new ServiceError('업로드가 100 MB를 초과했습니다.', 413);
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

// HTTP upload adapter: a future presigned URL adapter can produce the same file tree.
async function uploadedSource(request, sourceLoader) {
  const contentType = request.headers['content-type'] || '';
  if (!contentType.startsWith('multipart/form-data;')) throw new ServiceError('multipart/form-data 요청이 필요합니다.', 415);
  const body = await readLimited(request, archiveLimits.maxBytes + 1024 * 1024);
  const form = await new Request('http://localhost/api/deploy', {
    method: 'POST', headers: { 'content-type': contentType }, body,
  }).formData();
  const app = form.get('app');
  if (typeof app !== 'string') throw new ServiceError('앱 이름이 필요합니다.', 400);
  if (!/^[a-z0-9-]{1,30}$/.test(app)) throw new ServiceError('앱 이름은 소문자, 숫자, 하이픈으로 1~30자여야 합니다.', 400);
  const uploads = form.getAll('files');
  const supplied = [
    form.has('repository_url') && 'github',
    uploads.length > 0 && 'folder',
    form.has('archive') && 'zip',
  ].filter(Boolean);
  if (supplied.length !== 1) throw new ServiceError('배포 소스 하나만 입력하세요.', 400);
  const sourceType = supplied[0];
  if (form.has('source_type') && form.get('source_type') !== sourceType) throw new ServiceError('소스 형식과 입력값이 일치하지 않습니다.', 400);
  if (sourceType === 'github') {
    const repositoryUrl = form.get('repository_url');
    if (typeof repositoryUrl !== 'string') throw new ServiceError('공개 GitHub 저장소 URL이 필요합니다.', 400);
    return { app, ...await sourceLoader(repositoryUrl) };
  }
  if (sourceType === 'folder') {
    let paths;
    try { paths = JSON.parse(form.get('paths')); } catch { throw new ServiceError('폴더 파일 경로가 잘못되었습니다.', 400); }
    if (!Array.isArray(paths) || paths.length !== uploads.length || uploads.length > archiveLimits.maxFiles) {
      throw new ServiceError('폴더 파일 목록이 잘못되었습니다.', 400);
    }
    const files = await Promise.all(uploads.map(async (file, index) => {
      if (!file || typeof file.arrayBuffer !== 'function') throw new ServiceError('폴더 파일이 잘못되었습니다.', 400);
      return { path: paths[index], content: Buffer.from(await file.arrayBuffer()) };
    }));
    return { app, files: validateFiles(files) };
  }
  const file = form.get('archive');
  if (!file || typeof file.arrayBuffer !== 'function') throw new ServiceError('ZIP 파일이 필요합니다.', 400);
  if (!file.name?.toLowerCase().endsWith('.zip')) throw new ServiceError('ZIP 파일만 업로드할 수 있습니다.', 400);
  return { app, files: await inspectArchive(Buffer.from(await file.arrayBuffer())) };
}

export function createAppServer({ sourceLoader = fetchPublicGithubSource, service = process.env.GITHUB_TOKEN ? createDeploymentService({
  token: process.env.GITHUB_TOKEN,
  owner: process.env.GITHUB_OWNER,
  repo: process.env.GITHUB_REPO,
  ref: process.env.GITHUB_REF,
  tenant: process.env.JASMIN_TENANT,
  workflow: process.env.GITHUB_WORKFLOW,
}) : null } = {}) {
  return createServer(async (request, response) => {
    const host = request.headers.host?.split(':')[0];
    if (host !== 'localhost' && host !== '127.0.0.1') { response.writeHead(403).end('Local access only'); return; }
    const origin = request.headers.origin;
    if (origin && !/^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(origin)) {
      json(response, 403, { error: '다른 사이트에서는 접근할 수 없습니다.' }); return;
    }
    const url = new URL(request.url, 'http://localhost');
    if (request.method === 'GET' && url.pathname === '/healthz') {
      json(response, 200, { ok: true, configured: Boolean(service) }); return;
    }
    if (url.pathname.startsWith('/api/')) {
      if (!service) { json(response, 503, { error: 'GITHUB_TOKEN이 설정되지 않았습니다.' }); return; }
      try {
        if (request.method === 'POST' && url.pathname === '/api/deploy') {
          // A non-simple header forces a browser CORS preflight for cross-site requests.
          if (request.headers['x-jasmin-request'] !== 'deploy') throw new ServiceError('요청 헤더가 필요합니다.', 403);
          json(response, 202, await service.deploy(await uploadedSource(request, sourceLoader)));
          return;
        }
        const match = request.method === 'GET' && /^\/api\/runs\/(\d+)$/.exec(url.pathname);
        if (match) { json(response, 200, await service.status(match[1])); return; }
        json(response, 404, { error: 'API 경로를 찾을 수 없습니다.' });
      } catch (error) {
        json(response, error.status || 400, { error: error.message || '요청을 처리하지 못했습니다.' });
      }
      return;
    }
    const asset = request.method === 'GET' && assets.get(url.pathname);
    if (!asset) { response.writeHead(404).end('Not found'); return; }
    try {
      const content = await readFile(join(root, asset[0]));
      response.writeHead(200, {
        'content-type': asset[1], 'content-length': content.length,
        'cache-control': 'no-store', 'x-content-type-options': 'nosniff',
      }).end(content);
    } catch { response.writeHead(500).end('Unable to load asset'); }
  });
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const port = Number(process.env.PORT || 4173);
  createAppServer().listen(port, '127.0.0.1', () => {
    console.log(`RAILSHOT PoC: http://127.0.0.1:${port}`);
  });
}
