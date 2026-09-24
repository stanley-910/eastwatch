#!/usr/bin/env node

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

function argumentsByName(argv) {
  const values = new Map();
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (!flag?.startsWith('--') || value === undefined) {
      throw new Error(`expected --name value pairs; stopped at ${flag || '<end>'}`);
    }
    values.set(flag.slice(2), value);
  }
  return values;
}

function safeValue(name, value) {
  if (!value || /[\r\n\0]/.test(value)) {
    throw new Error(`${name} must be non-empty and single-line`);
  }
  return value;
}

const args = argumentsByName(process.argv.slice(2));
const required = ['ssh-target', 'ssh-alias', 'owner', 'server-command', 'editor'];
for (const name of required) {
  if (!args.has(name)) throw new Error(`missing --${name}`);
}
const output = path.resolve(
  args.get('output') || path.join(os.homedir(), '.config', 'eastwatch', 'remote.yaml'),
);
const values = {
  ssh_target: safeValue('ssh-target', args.get('ssh-target')),
  ssh_alias: safeValue('ssh-alias', args.get('ssh-alias')),
  owner: safeValue('owner', args.get('owner')),
  server_command: safeValue('server-command', args.get('server-command')),
  editor: safeValue('editor', args.get('editor')),
};
const payload = `${Object.entries(values)
  .map(([name, value]) => `${name}: ${JSON.stringify(value)}`)
  .join('\n')}\n`;

fs.mkdirSync(path.dirname(output), { recursive: true, mode: 0o700 });
let backup = null;
if (fs.existsSync(output)) {
  const current = fs.readFileSync(output, 'utf8');
  if (current === payload) {
    console.log(`remote config already current: ${output}`);
    process.exit(0);
  }
  backup = `${output}.bak`;
  fs.copyFileSync(output, backup);
}
fs.writeFileSync(output, payload, { encoding: 'utf8', mode: 0o600 });
try {
  fs.chmodSync(output, 0o600);
} catch (error) {
  if (process.platform !== 'win32') throw error;
}
console.log(`wrote remote config: ${output}`);
if (backup) console.log(`previous config: ${backup}`);
