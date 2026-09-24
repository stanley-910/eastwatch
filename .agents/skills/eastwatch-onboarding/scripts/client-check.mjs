#!/usr/bin/env node

import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

function value(flag, fallback) {
  const index = process.argv.indexOf(flag);
  return index >= 0 ? process.argv[index + 1] : fallback;
}

function commandStatus(command, args) {
  const result = spawnSync(command, args, { encoding: 'utf8', windowsHide: true });
  const output = `${result.stdout || ''}${result.stderr || ''}`.trim().split('\n')[0];
  return { ok: result.status === 0, output };
}

const repo = path.resolve(value('--repo', process.cwd()));
const config = path.resolve(
  value('--remote-config', path.join(os.homedir(), '.config', 'eastwatch', 'remote.yaml')),
);
const checks = [
  ['git', 'Git', ['--version']],
  ['ssh', 'OpenSSH', ['-V']],
  ['node', 'Node', ['--version']],
  ['pi', 'Pi', ['--version']],
  ['uv', 'uv', ['--version']],
];

console.log(`platform: ${process.platform} ${process.arch}`);
console.log(`home: ${os.homedir()}`);
let failed = false;
for (const [command, label, args] of checks) {
  const result = commandStatus(command, args);
  console.log(`${result.ok ? 'OK' : 'MISSING'} ${label}${result.output ? ` — ${result.output}` : ''}`);
  failed ||= !result.ok;
}

const repoChecks = [
  path.join(repo, 'pyproject.toml'),
  path.join(repo, 'deploy', 'host', 'onboard-workspace.sh'),
  path.join(repo, 'ONBOARDING.md'),
];
const repoReady = repoChecks.every((candidate) => fs.existsSync(candidate));
console.log(`${repoReady ? 'OK' : 'MISSING'} eastwatch checkout — ${repo}`);
console.log(`${fs.existsSync(config) ? 'OK' : 'PENDING'} remote config — ${config}`);
failed ||= !repoReady;

if (failed) {
  console.error('client preflight incomplete; install missing tools or clone Eastwatch, then rerun');
  process.exitCode = 1;
}
