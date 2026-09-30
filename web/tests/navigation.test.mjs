import assert from 'node:assert/strict';
import test from 'node:test';
import { readRoute, routeHash, sections } from '../src/navigation.ts';

test('existing saved links open their consolidated destination', () => {
  const expected = {
    chat: 'workspace/chat', runs: 'workspace/history', knowledge: 'documents/library',
    seal: 'assurance/network', audit: 'assurance/activity', evals: 'assurance/evaluations',
    models: 'settings/models', settings: 'settings/runtime',
  };
  for (const [old, destination] of Object.entries(expected)) {
    assert.equal(routeHash(readRoute(`#${old}`)), `#${destination}`);
  }
});

test('every section and tab survives a direct link or refresh', () => {
  for (const [section, info] of Object.entries(sections)) {
    for (const tab of Object.keys(info.tabs)) {
      assert.deepEqual(readRoute(`#${section}/${tab}`), { section, tab });
    }
  }
});

test('unknown and inherited property names cannot crash navigation', () => {
  for (const hash of ['', '#missing', '#__proto__', '#constructor', '#toString', '#%ZZ']) {
    assert.deepEqual(readRoute(hash), { section: 'workspace', tab: 'chat' });
  }
  assert.deepEqual(readRoute('#assurance/constructor'), { section: 'assurance', tab: 'network' });
});
