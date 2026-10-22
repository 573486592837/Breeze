'use strict';

// 校验 multiply 已恢复为真正的乘法。
const assert = require('node:assert');
const { multiply } = require('./src/index');

assert.strictEqual(multiply(2, 3), 6);
assert.strictEqual(multiply(3, 4), 12);
assert.strictEqual(multiply(-2, 5), -10);
assert.strictEqual(multiply(0, 7), 0);

console.log('multiply checks passed');
