import { createHash } from 'node:crypto';
import { inspectArchive, validateFiles } from './archive.js';

const API = 'https://api.github.com';

export class ServiceError extends Error {
  constructor(message, status = 500) { super(message); this.status = status; }
}

export function createDeploymentService(config, fetchImpl = fetch) {
  const { token, owner = 'Jasmin-Softbank', repo = 'railshot-apps', ref = 'main', tenant = 'demo', workflow = 'railshot-deploy.yml' } = config;
  if (!token) throw new Error('GITHUB_TOKEN을 설정하세요.');
  if (!/^[a-z0-9-]{1,30}$/.test(tenant)) throw new Error('JASMIN_TENANT가 잘못되었습니다.');
  const repoPath = `/repos/${owner}/${repo}`;

  async function request(path, options = {}) {
    const response = await fetchImpl(`${API}${path}`, {
      ...options,
      headers: {
        accept: 'application/vnd.github+json',
        authorization: `Bearer ${token}`,
        'x-github-api-version': '2026-03-10',
        ...(options.body ? { 'content-type': 'application/json' } : {}),
        ...options.headers,
      },
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new ServiceError(`GitHub API ${response.status}: ${body.message || '요청 실패'}`, [404, 409].includes(response.status) ? response.status : 502);
    }
    return response.status === 204 ? {} : response.json();
  }

  async function findAppTree(rootSha, app) {
    let sha = rootSha;
    for (const segment of ['apps', tenant, app]) {
      const parent = await request(`${repoPath}/git/trees/${sha}`);
      const entry = parent.tree.find((item) => item.path === segment);
      if (!entry) return null;
      if (entry.type !== 'tree') throw new ServiceError(`앱 경로가 디렉터리가 아닙니다: ${segment}`, 409);
      sha = entry.sha;
    }
    const tree = await request(`${repoPath}/git/trees/${sha}?recursive=1`);
    if (tree.truncated) throw new ServiceError('기존 앱 파일 목록이 너무 커서 안전하게 갱신할 수 없습니다.', 409);
    return tree.tree.filter((item) => item.type !== 'tree');
  }

  function blobSha(content) {
    return createHash('sha1').update(`blob ${content.length}\0`).update(content).digest('hex');
  }

  async function deploy({ app, files, source }) {
    if (!/^[a-z0-9-]{1,30}$/.test(app)) throw new ServiceError('앱 이름은 소문자, 숫자, 하이픈으로 1~30자여야 합니다.', 400);
    const acceptedFiles = validateFiles(files);
    const prefix = `apps/${tenant}/${app}`;
    const branch = await request(`${repoPath}/git/ref/heads/${encodeURIComponent(ref)}`);
    const parent = branch.object.sha;
    const base = await request(`${repoPath}/git/commits/${parent}`);
    const existing = await findAppTree(base.tree.sha, app);
    const previous = new Map(existing?.map((item) => [item.path, item]) || []);
    const changes = { added: 0, updated: 0, deleted: 0, unchanged: 0 };
    const treeEntries = [];
    for (const file of acceptedFiles) {
      const before = previous.get(file.path);
      let sha = blobSha(file.content);
      if (!before) changes.added++;
      else if (before.sha !== sha || before.mode !== '100644' || before.type !== 'blob') changes.updated++;
      else changes.unchanged++;
      if (before?.sha !== sha || before.type !== 'blob') {
        const blob = await request(`${repoPath}/git/blobs`, {
          method: 'POST',
          body: JSON.stringify({ content: file.content.toString('base64'), encoding: 'base64' }),
        });
        sha = blob.sha;
      }
      treeEntries.push({ path: file.path, mode: '100644', type: 'blob', sha });
    }
    const incomingPaths = new Set(acceptedFiles.map((file) => file.path));
    changes.deleted = [...previous.keys()].filter((path) => !incomingPaths.has(path)).length;
    if (changes.added || changes.updated || changes.deleted) {
      // Replace only this app's tree; absent paths disappear, while other apps stay on base_tree.
      const appTree = await request(`${repoPath}/git/trees`, {
        method: 'POST', body: JSON.stringify({ tree: treeEntries }),
      });
      const tree = await request(`${repoPath}/git/trees`, {
        method: 'POST', body: JSON.stringify({ base_tree: base.tree.sha, tree: [
          { path: prefix, mode: '040000', type: 'tree', sha: appTree.sha },
        ] }),
      });
      const commit = await request(`${repoPath}/git/commits`, {
        method: 'POST',
        body: JSON.stringify({ message: `${existing ? 'fix: update' : 'feat: add'} ${tenant}/${app} via entrypoints PoC${source?.type === 'github' ? `\n\nSource: ${source.repository}@${source.sha}` : ''}`, tree: tree.sha, parents: [parent] }),
      });
      await request(`${repoPath}/git/refs/heads/${encodeURIComponent(ref)}`, {
        method: 'PATCH', body: JSON.stringify({ sha: commit.sha, force: false }),
      });
    }
    const dispatched = await request(`${repoPath}/actions/workflows/${encodeURIComponent(workflow)}/dispatches`, {
      method: 'POST', body: JSON.stringify({ ref, inputs: { tenant, app } }),
    });
    if (!dispatched.workflow_run_id) {
      throw new ServiceError('앱은 등록됐지만 Actions 실행 ID를 받지 못했습니다. GitHub Actions를 확인하세요.', 502);
    }
    return {
      run_id: dispatched.workflow_run_id, tenant, app, changes, ...(source ? { source } : {}),
      actions_url: dispatched.html_url || `https://github.com/${owner}/${repo}/actions/runs/${dispatched.workflow_run_id}`,
    };
  }

  async function status(runId) {
    if (!/^\d+$/.test(String(runId))) throw new ServiceError('run_id가 잘못되었습니다.', 400);
    const run = await request(`${repoPath}/actions/runs/${runId}`);
    if (run.path && !run.path.endsWith(`/${workflow}`)) throw new ServiceError('해당 실행은 배포 워크플로가 아닙니다.', 404);
    const jobs = await request(`${repoPath}/actions/runs/${runId}/jobs?per_page=100`);
    const steps = ['loop', 'release', 'gitops'].map((key) => {
      const job = jobs.jobs.find((item) => item.name === key);
      return {
        key,
        status: job?.status || (run.status === 'completed' ? 'completed' : 'queued'),
        conclusion: job?.conclusion || (run.status === 'completed' ? 'skipped' : null),
      };
    });
    const conclusion = run.status === 'completed' && run.conclusion === 'success' && steps.some((step) => step.conclusion !== 'success')
      ? 'failure' : run.conclusion;
    let url = null;
    if (run.status === 'completed' && conclusion === 'success') {
      try { url = await renderedUrl(runId); } catch { /* A successful run may have an expired artifact. */ }
    }
    return {
      run_id: Number(runId), app: null, tenant: null,
      status: run.status, conclusion,
      actions_url: run.html_url, steps, url,
      message: conclusion === 'failure' ? '배포가 완료되지 않았습니다. GitHub Actions 로그를 확인하세요.' : null,
    };
  }

  async function renderedUrl(runId) {
    const list = await request(`${repoPath}/actions/runs/${runId}/artifacts?name=rendered`);
    const artifact = list.artifacts?.find((item) => item.name === 'rendered' && !item.expired);
    if (!artifact) return null;
    const response = await fetchImpl(artifact.archive_download_url, {
      headers: { authorization: `Bearer ${token}`, accept: 'application/vnd.github+json' },
    });
    if (!response.ok) return null;
    const files = await inspectArchive(Buffer.from(await response.arrayBuffer()));
    const render = files.find((file) => file.path === 'render.json');
    const value = render && JSON.parse(render.content.toString('utf8')).url;
    return typeof value === 'string' && /^https?:\/\//.test(value) ? value : null;
  }

  return { deploy, status };
}
