'use strict';

// 校验 exports.md 是否正确梳理了样例各模块的导出。
const fs = require('node:fs');
const path = require('node:path');

const ANSWER = path.join(__dirname, 'exports.md');
const REQUIRED = [
  'index.js: add',
  'index.js: subtract',
  'index.js: multiply',
  'stats.js: mean',
  'stats.js: median',
  'text.js: capitalize',
];

let text;
try {
  text = fs.readFileSync(ANSWER, 'utf8');
} catch {
  console.error('missing samples/node-app/exports.md');
  process.exit(1);
}

const lines = text
  .split(/\r?\n/)
  .map((l) => l.trim())
  .filter((l) => l.length > 0);

let failed = false;
for (const token of REQUIRED) {
  if (!lines.some((l) => l.includes(token))) {
    console.error(`exports.md missing entry: ${token}`);
    failed = true;
  }
}

const last = lines[lines.length - 1] || '';
if (!last.includes('node run_checks.js')) {
  console.error('last line of exports.md must mention: node run_checks.js');
  failed = true;
}

if (failed) {
  process.exit(1);
}
console.log('exports.md checks passed');
