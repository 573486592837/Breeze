'use strict';

const assert = require('node:assert');
const { add, subtract, multiply } = require('./src/index');
const { mean, median } = require('./src/stats');
const { capitalize } = require('./src/text');

assert.strictEqual(add(2, 3), 5);
assert.strictEqual(subtract(7, 4), 3);
assert.strictEqual(multiply(2, 3), 6);
assert.strictEqual(mean([1, 2, 3]), 2);
assert.strictEqual(median([3, 1, 2]), 2);
assert.strictEqual(median([4, 1, 2, 3]), 2.5);
assert.throws(() => mean([]), /empty/);
assert.strictEqual(capitalize('hello'), 'Hello');
assert.strictEqual(capitalize(''), '');

console.log('all checks passed');
