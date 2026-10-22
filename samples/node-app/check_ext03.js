'use strict';

// 校验 src/math2.js 已创建且 square/cube 行为正确。
const assert = require('node:assert');

let mod;
try {
  mod = require('./src/math2');
} catch (err) {
  console.error(`cannot require samples/node-app/src/math2.js: ${err.message}`);
  process.exit(1);
}

const { square, cube } = mod;
assert.strictEqual(typeof square, 'function');
assert.strictEqual(typeof cube, 'function');
assert.strictEqual(square(5), 25);
assert.strictEqual(square(0), 0);
assert.strictEqual(square(-3), 9);
assert.strictEqual(cube(3), 27);
assert.strictEqual(cube(-2), -8);

console.log('math2 checks passed');
