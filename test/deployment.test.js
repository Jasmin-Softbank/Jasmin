import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import yazl from 'yazl';
import { mkdtemp, mkdir, symlink, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { inspectArchive } from '../src/archive.js';
import { createDeploymentService } from '../src/github.js';
import { createAppServer } from '../src/server.js';
import { archiveFromPath, deploySource, inferredAppName, insideRoot } from '../src/client.js';
import { fetchPublicGithubSource } from '../src/public-github.js';

async function zipOf(files) {
  const zip = new yazl.ZipFile();
  for (const [name, content] of Object.entries(files)) zip.addBuffer(Buffer.from(content), name);
  zip.end();
  const chunks = [];
  for await (const chunk of zip.outputStream) chunks.push(chunk);
  return Buffer.concat(chunks);
}

test('ZIP 검사 후 앱을 Git 트리에 등록하고 Actions 실행 ID를 반환한다', async () => {
  const archive = await zipOf({ 'my-app/package.json': '{"name":"test"}', 'my-app/server.js': 'hello' });
  const calls = [];
  const fakeFetch = async (url, options = {}) => {
    const path = new URL(url).pathname;
    calls.push({ path, method: options.method || 'GET', body: options.body && JSON.parse(options.body) });
    let data;
    let status = 200;
    if (path.endsWith('/git/ref/heads/main')) data = { object: { sha: 'parent' } };
    else if (path.endsWith('/git/commits/parent')) data = { tree: { sha: 'base' } };
    else if (path.endsWith('/git/trees/base')) data = { tree: [] };
    else if (path.endsWith('/git/blobs')) data = { sha: `blob-${calls.length}` };
    else if (path.endsWith('/git/trees')) data = { sha: 'tree' };
    else if (path.endsWith('/git/commits')) data = { sha: 'commit' };
    else if (path.endsWith('/git/refs/heads/main')) data = {};
    else if (path.endsWith('/dispatches')) data = { workflow_run_id: 123, html_url: 'https://github.com/example/run/123' };
    else throw new Error(`Unexpected path: ${path}`);
    return new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } });
  };
  const service = createDeploymentService({ token: 'test', owner: 'org', repo: 'apps' }, fakeFetch);
  const result = await service.deploy({ app: 'my-app', files: await inspectArchive(archive) });
  assert.equal(result.run_id, 123);
  assert.deepEqual(result.changes, { added: 2, updated: 0, deleted: 0, unchanged: 0 });
  const trees = calls.filter((call) => call.path.endsWith('/git/trees') && call.method === 'POST').map((call) => call.body);
  assert.deepEqual(trees[0].tree.map((item) => item.path), ['package.json', 'server.js']);
  assert.equal(trees[1].tree[0].path, 'apps/demo/my-app');
  const dispatch = calls.find((call) => call.path.endsWith('/dispatches')).body;
  assert.deepEqual(dispatch.inputs, { tenant: 'demo', app: 'my-app' });
});

test('재배포는 변경된 blob만 올리고 삭제된 파일은 앱 트리에서 제외한다', async () => {
  const sha = (value) => createHash('sha1').update(`blob ${Buffer.byteLength(value)}\0${value}`).digest('hex');
  const calls = [];
  const oldFiles = [
    { path: 'same.txt', mode: '100644', type: 'blob', sha: sha('same') },
    { path: 'changed.txt', mode: '100644', type: 'blob', sha: sha('old') },
    { path: 'removed.txt', mode: '100644', type: 'blob', sha: sha('removed') },
  ];
  const fakeFetch = async (url, options = {}) => {
    const path = new URL(url).pathname;
    const method = options.method || 'GET';
    calls.push({ path, method, body: options.body && JSON.parse(options.body) });
    let data;
    if (path.endsWith('/git/ref/heads/main') && method === 'GET') data = { object: { sha: 'parent' } };
    else if (path.endsWith('/git/commits/parent')) data = { tree: { sha: 'base' } };
    else if (path.endsWith('/git/trees/base')) data = { tree: [{ path: 'apps', type: 'tree', sha: 'apps-tree' }] };
    else if (path.endsWith('/git/trees/apps-tree')) data = { tree: [{ path: 'demo', type: 'tree', sha: 'tenant-tree' }] };
    else if (path.endsWith('/git/trees/tenant-tree')) data = { tree: [{ path: 'my-app', type: 'tree', sha: 'app-tree' }] };
    else if (path.endsWith('/git/trees/app-tree')) data = { tree: oldFiles, truncated: false };
    else if (path.endsWith('/git/blobs')) data = { sha: 'new-blob' };
    else if (path.endsWith('/git/trees') && method === 'POST') data = { sha: 'new-tree' };
    else if (path.endsWith('/git/commits') && method === 'POST') data = { sha: 'new-commit' };
    else if (path.endsWith('/git/refs/heads/main') && method === 'PATCH') data = {};
    else if (path.endsWith('/dispatches')) data = { workflow_run_id: 456 };
    else throw new Error(`Unexpected path: ${path}`);
    return Response.json(data);
  };
  const service = createDeploymentService({ token: 'test', owner: 'org', repo: 'apps' }, fakeFetch);
  const result = await service.deploy({ app: 'my-app', files: [
    { path: 'same.txt', content: Buffer.from('same') },
    { path: 'changed.txt', content: Buffer.from('new') },
    { path: 'added.txt', content: Buffer.from('added') },
  ] });
  assert.deepEqual(result.changes, { added: 1, updated: 1, deleted: 1, unchanged: 1 });
  assert.equal(calls.filter((call) => call.path.endsWith('/git/blobs')).length, 2);
  const appTree = calls.find((call) => call.path.endsWith('/git/trees') && call.method === 'POST').body;
  assert.deepEqual(appTree.tree.map((item) => item.path), ['same.txt', 'changed.txt', 'added.txt']);
  assert.equal(appTree.tree[0].sha, oldFiles[0].sha);
  assert.equal(calls.at(-1).path.endsWith('/dispatches'), true);
});

test('소스가 같아도 커밋 없이 Actions를 다시 실행한다', async () => {
  const content = Buffer.from('same');
  const sha = createHash('sha1').update(`blob ${content.length}\0`).update(content).digest('hex');
  const calls = [];
  const fakeFetch = async (url, options = {}) => {
    const path = new URL(url).pathname;
    calls.push({ path, method: options.method || 'GET' });
    let data;
    if (path.endsWith('/git/ref/heads/main')) data = { object: { sha: 'parent' } };
    else if (path.endsWith('/git/commits/parent')) data = { tree: { sha: 'base' } };
    else if (path.endsWith('/git/trees/base')) data = { tree: [{ path: 'apps', type: 'tree', sha: 'apps-tree' }] };
    else if (path.endsWith('/git/trees/apps-tree')) data = { tree: [{ path: 'demo', type: 'tree', sha: 'tenant-tree' }] };
    else if (path.endsWith('/git/trees/tenant-tree')) data = { tree: [{ path: 'my-app', type: 'tree', sha: 'app-tree' }] };
    else if (path.endsWith('/git/trees/app-tree')) data = { tree: [{ path: 'same.txt', mode: '100644', type: 'blob', sha }], truncated: false };
    else if (path.endsWith('/dispatches')) data = { workflow_run_id: 789 };
    else throw new Error(`Unexpected path: ${path}`);
    return Response.json(data);
  };
  const service = createDeploymentService({ token: 'test', owner: 'org', repo: 'apps' }, fakeFetch);
  const result = await service.deploy({ app: 'my-app', files: [{ path: 'same.txt', content }] });
  assert.deepEqual(result.changes, { added: 0, updated: 0, deleted: 0, unchanged: 1 });
  assert.deepEqual(calls.filter((call) => call.method !== 'GET').map((call) => call.path.split('/').at(-1)), ['dispatches']);
});

test('ZIP 경로 이동과 비밀키 파일을 거부한다', async () => {
  await assert.rejects(inspectArchive(await zipOf({ 'app/.env': 'secret' })), /비밀키/);
  const unsafe = await zipOf({ 'aaa/outside': 'bad' });
  const text = unsafe.toString('latin1').replaceAll('aaa/outside', '../.outside');
  await assert.rejects(inspectArchive(Buffer.from(text, 'latin1')), /invalid relative path|안전하지 않은/);
});

test('공개 GitHub 저장소의 기본 브랜치를 SHA로 고정하고 공통 파일 목록으로 변환한다', async () => {
  const sha = 'a'.repeat(40);
  const zip = await zipOf({ 'sample-a1b2c3/requirements.txt': 'flask', 'sample-a1b2c3/app.py': 'print(1)' });
  const calls = [];
  const fakeFetch = async (url, options) => {
    calls.push({ url: String(url), options });
    if (url === 'https://api.github.com/repos/example/sample') return Response.json({ private: false, visibility: 'public', default_branch: 'main' });
    if (url === 'https://api.github.com/repos/example/sample/commits/main') return Response.json({ sha });
    if (url === `https://api.github.com/repos/example/sample/zipball/${sha}`) {
      return new Response(null, { status: 302, headers: { location: `https://codeload.github.com/example/sample/legacy.zip/${sha}` } });
    }
    if (url.href === `https://codeload.github.com/example/sample/legacy.zip/${sha}`) return new Response(zip);
    throw new Error(`Unexpected URL: ${url}`);
  };
  const result = await fetchPublicGithubSource('https://github.com/example/sample.git', fakeFetch);
  assert.deepEqual(result.files.map((file) => file.path), ['requirements.txt', 'app.py']);
  assert.deepEqual(result.source, { type: 'github', repository: 'https://github.com/example/sample', sha });
  assert.ok(calls.every((call) => !call.options.headers.authorization));
});

test('GitHub 입력은 공개 저장소 기본 URL로만 제한한다', async () => {
  await assert.rejects(fetchPublicGithubSource('http://github.com/example/sample'), /공개 저장소 URL/);
  await assert.rejects(fetchPublicGithubSource('https://github.com/example/sample/tree/main'), /기본 URL/);
  await assert.rejects(fetchPublicGithubSource('https://github.com/example/sample?token=x'), /공개 저장소 URL/);
  await assert.rejects(fetchPublicGithubSource('https://github.com/example/sample', async () => Response.json({ private: true, default_branch: 'main' })), /공개 저장소만/);
});

test('소스 이름에서 앱 이름을 만들고 잘못된 이름은 거부한다', () => {
  assert.equal(inferredAppName('/tmp/My App.zip'), 'my-app');
  assert.equal(inferredAppName('https://github.com/example/Web.App.git'), 'web-app');
  assert.throws(() => inferredAppName('/tmp/앱.zip'), /앱 이름/);
});

test('HTTP 업로드, GitHub URL과 상태 조회는 동일한 서비스를 사용한다', async () => {
  const observed = [];
  const server = createAppServer({ sourceLoader: async (url) => ({
    files: [{ path: 'app.py', content: Buffer.from('print(1)') }],
    source: { type: 'github', repository: url, sha: 'b'.repeat(40) },
  }), service: {
    deploy: async (input) => { observed.push(input); return { run_id: 456, app: input.app, tenant: 'demo' }; },
    status: async (id) => ({ run_id: Number(id), status: 'queued', steps: [] }),
  } });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    const base = `http://127.0.0.1:${server.address().port}`;
    const form = new FormData();
    form.set('app', 'my-app');
    form.set('archive', new Blob([await zipOf({ 'index.js': 'test' })]), 'app.zip');
    const create = await fetch(`${base}/api/deploy`, { method: 'POST', headers: { 'x-jasmin-request': 'deploy' }, body: form });
    assert.equal(create.status, 202);
    assert.equal((await create.json()).run_id, 456);
    assert.equal(observed[0].app, 'my-app');
    assert.deepEqual(observed[0].files.map((file) => file.path), ['index.js']);
    const folder = new FormData();
    folder.set('app', 'folder-app');
    folder.append('files', new Blob(['hello']), 'index.js');
    folder.set('paths', JSON.stringify(['src/index.js']));
    const folderCreate = await fetch(`${base}/api/deploy`, { method: 'POST', headers: { 'x-jasmin-request': 'deploy' }, body: folder });
    assert.equal(folderCreate.status, 202);
    assert.deepEqual(observed[1].files.map((file) => file.path), ['src/index.js']);
    folder.set('paths', JSON.stringify(['../outside.js']));
    const unsafeFolder = await fetch(`${base}/api/deploy`, { method: 'POST', headers: { 'x-jasmin-request': 'deploy' }, body: folder });
    assert.equal(unsafeFolder.status, 400);
    assert.equal(observed.length, 2);
    const github = new FormData();
    github.set('app', 'github-app');
    github.set('repository_url', 'https://github.com/example/sample');
    const githubCreate = await fetch(`${base}/api/deploy`, { method: 'POST', headers: { 'x-jasmin-request': 'deploy' }, body: github });
    assert.equal(githubCreate.status, 202);
    assert.equal(observed[2].files[0].path, 'app.py');
    assert.equal(observed[2].source.sha, 'b'.repeat(40));
    assert.equal((await deploySource({ source: 'https://github.com/example/sample', baseUrl: base })).app, 'sample');
    assert.equal(observed[3].app, 'sample');
    const localFolder = await mkdtemp(join(tmpdir(), 'jasmin-local-'));
    try {
      await writeFile(join(localFolder, 'index.js'), 'console.log(1)');
      assert.equal((await deploySource({ source: localFolder, baseUrl: base })).app, inferredAppName(localFolder));
      assert.deepEqual(observed[4].files.map((file) => file.path), ['index.js']);
    } finally { await rm(localFolder, { recursive: true, force: true }); }
    github.set('archive', new Blob([await zipOf({ 'index.js': 'test' })]), 'app.zip');
    const multiple = await fetch(`${base}/api/deploy`, { method: 'POST', headers: { 'x-jasmin-request': 'deploy' }, body: github });
    assert.equal(multiple.status, 400);
    assert.equal(observed.length, 5);
    const status = await fetch(`${base}/api/runs/456`);
    assert.equal((await status.json()).status, 'queued');
    const blocked = await fetch(`${base}/api/deploy`, { method: 'POST', body: form });
    assert.equal(blocked.status, 403);
  } finally { server.close(); }
});

test('완료된 Actions 실행의 작업과 render.json URL을 조회한다', async () => {
  const artifact = await zipOf({ 'render.json': '{"url":"https://my-app.example"}' });
  const fakeFetch = async (url) => {
    const path = new URL(url).pathname;
    let value;
    if (path.endsWith('/actions/runs/789')) value = {
      status: 'completed', conclusion: 'success', path: '.github/workflows/railshot-deploy.yml',
      html_url: 'https://github.com/org/apps/actions/runs/789',
    };
    else if (path.endsWith('/actions/runs/789/jobs')) value = { jobs: [
      { name: 'loop', status: 'completed', conclusion: 'success' },
      { name: 'release', status: 'completed', conclusion: 'success' },
      { name: 'gitops', status: 'completed', conclusion: 'success' },
    ] };
    else if (path.endsWith('/actions/runs/789/artifacts')) value = { artifacts: [
      { name: 'rendered', expired: false, archive_download_url: 'https://archive.example/rendered.zip' },
    ] };
    else if (url === 'https://archive.example/rendered.zip') return new Response(artifact);
    else throw new Error(`Unexpected path: ${path}`);
    return new Response(JSON.stringify(value), { headers: { 'content-type': 'application/json' } });
  };
  const service = createDeploymentService({ token: 'test', owner: 'org', repo: 'apps' }, fakeFetch);
  const result = await service.status('789');
  assert.equal(result.url, 'https://my-app.example');
  assert.deepEqual(result.steps.map((step) => step.conclusion), ['success', 'success', 'success']);
});

test('Actions가 성공 표시여도 release나 gitops가 건너뛰어졌으면 배포 실패로 표시한다', async () => {
  const fakeFetch = async (url) => {
    const path = new URL(url).pathname;
    if (path.endsWith('/actions/runs/789')) return Response.json({
      status: 'completed', conclusion: 'success', path: '.github/workflows/railshot-deploy.yml',
      html_url: 'https://github.com/org/apps/actions/runs/789',
    });
    if (path.endsWith('/actions/runs/789/jobs')) return Response.json({ jobs: [
      { name: 'loop', status: 'completed', conclusion: 'success' },
      { name: 'release', status: 'completed', conclusion: 'skipped' },
      { name: 'gitops', status: 'completed', conclusion: 'skipped' },
    ] });
    throw new Error(`Unexpected path: ${path}`);
  };
  const service = createDeploymentService({ token: 'test', owner: 'org', repo: 'apps' }, fakeFetch);
  const result = await service.status('789');
  assert.equal(result.conclusion, 'failure');
  assert.equal(result.url, null);
});

test('MCP 소스 경로는 심볼릭 링크를 통해 허용 범위 밖으로 나갈 수 없다', async () => {
  const parent = await mkdtemp(join(tmpdir(), 'jasmin-poc-'));
  try {
    const allowed = join(parent, 'allowed');
    const outside = join(parent, 'outside');
    await mkdir(allowed); await mkdir(outside);
    await symlink(outside, join(allowed, 'escape'));
    await assert.rejects(insideRoot(join(allowed, 'escape'), allowed), /허용된 소스 경로 밖/);
  } finally { await rm(parent, { recursive: true, force: true }); }
});

test('CLI의 폴더 입력은 API가 받는 ZIP으로 만들어진다', async () => {
  const folder = await mkdtemp(join(tmpdir(), 'jasmin-app-'));
  try {
    await writeFile(join(folder, 'package.json'), '{"name":"my-app"}');
    const archive = await archiveFromPath(folder);
    const files = await inspectArchive(archive.bytes);
    assert.deepEqual(files.map((file) => file.path), ['package.json']);
  } finally { await rm(folder, { recursive: true, force: true }); }
});
