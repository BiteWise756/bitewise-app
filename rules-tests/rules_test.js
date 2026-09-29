// Local Firestore Security Rules behavior tests.
// Runs inside `firebase emulators:exec --only firestore` (see package.json).
// Nothing here touches a real Firebase project.
//
// Design context: NutriLens talks to Firestore exclusively through the
// backend (Firebase Admin SDK, which bypasses rules). The frontend has NO
// Firebase Client SDK. These rules therefore deny ALL direct client access.
const { initializeTestEnvironment, assertFails } = require('@firebase/rules-unit-testing');
const fs = require('fs');
const path = require('path');
require('firebase/compat/firestore');

const RULES = fs.readFileSync(path.join(__dirname, '..', 'firestore.rules'), 'utf8');
const PROJECT = 'demo-nutrilens';

// Collections written exclusively by the backend.
const PRIVATE_COLLECTIONS = [
  'users', 'nutrition_plans', 'water_logs', 'weight_logs', 'meal_history',
  'reports', 'ai_history', 'doctor_notes', 'shopping_lists', 'favorites',
  'challenges', 'user_challenges',
];

let env;
let passed = 0;
let failed = 0;

function check(name, fn) {
  return fn()
    .then(() => { passed += 1; console.log('PASS  ' + name); })
    .catch((e) => { failed += 1; console.log('FAIL  ' + name + '  -> ' + e.message); });
}

async function main() {
  env = await initializeTestEnvironment({
    projectId: PROJECT,
    firestore: { rules: RULES, host: '127.0.0.1', port: 8080 },
  });

  // Seed a couple of "existing" documents the way the backend would create them.
  await env.withSecurityRulesDisabled(async (ctx) => {
    const db = ctx.firestore();
    await db.collection('users').doc('alice').set({ uid: 'alice', emailVerified: true, accountType: 'user' });
    await db.collection('users').doc('bob').set({ uid: 'bob', emailVerified: true, accountType: 'user' });
    await db.collection('water_logs').doc('w-bob').set({ userId: 'bob', amount: 250 });
    await db.collection('meal_history').doc('m-bob').set({ userId: 'bob', foodName: 'Burger' });
    await db.collection('challenges').doc('no-sugar').set({ name: 'No Sugar' });
  });

  // ---- 1. unauthenticated client access -> denied ----
  const anonDb = env.unauthenticatedContext().firestore();
  await check('1. unauth: read users/alice denied', () =>
    assertFails(anonDb.doc('users/alice').get()));
  await check('1. unauth: write users/alice denied', () =>
    assertFails(anonDb.doc('users/alice').set({ uid: 'alice' })));
  await check('1. unauth: delete users/alice denied', () =>
    assertFails(anonDb.doc('users/alice').delete()));
  await check('1. unauth: list users denied', () =>
    assertFails(anonDb.collection('users').get()));

  // ---- 2. authenticated client access to private collections -> denied ----
  const aliceDb = env.authenticatedContext('alice').firestore();
  for (const col of PRIVATE_COLLECTIONS) {
    await check(`2. authed alice: read ${col} denied`, () =>
      assertFails(aliceDb.collection(col).get()));
    await check(`2. authed alice: write ${col} denied`, () =>
      assertFails(aliceDb.collection(col).doc('anything').set({ x: 1 })));
  }

  // ---- 3. authenticated user cannot access another user's private data ----
  await check('3. alice: read bob user doc denied', () =>
    assertFails(aliceDb.doc('users/bob').get()));
  await check('3. alice: read bob water_log denied', () =>
    assertFails(aliceDb.doc('water_logs/w-bob').get()));
  await check('3. alice: read bob meal_history denied', () =>
    assertFails(aliceDb.doc('meal_history/m-bob').get()));
  await check('3. alice: query bob water_logs denied', () =>
    assertFails(aliceDb.collection('water_logs').where('userId', '==', 'bob').get()));

  // ---- 4. authenticated user cannot modify another user's data ----
  await check('4. alice: update bob user doc denied', () =>
    assertFails(aliceDb.doc('users/bob').update({ goal: 'hacked' })));
  await check('4. alice: delete bob water_log denied', () =>
    assertFails(aliceDb.doc('water_logs/w-bob').delete()));

  // Even a user's OWN documents are backend-managed: clients cannot write.
  await check('4. alice: update OWN users/alice denied (backend-owned)', () =>
    assertFails(aliceDb.doc('users/alice').update({ accountType: 'doctor' })));

  // ---- 5. unauthorized delete -> denied ----
  await check('5. authed: delete doctor_notes denied', () =>
    assertFails(aliceDb.collection('doctor_notes').doc('n1').delete()));
  await check('5. unauth: delete meal_history denied', () =>
    assertFails(anonDb.doc('meal_history/m-bob').delete()));

  // ---- 6. public challenge catalog read ----
  // The catalog is served by the backend (GET /challenges/ via Admin SDK),
  // so DIRECT client read is NOT required -> denied.
  await check('6. authed: read challenges catalog denied (not required; served via API)', () =>
    assertFails(aliceDb.doc('challenges/no-sugar').get()));

  // ---- 7. public challenge writes -> denied ----
  await check('7. authed: write challenges denied', () =>
    assertFails(aliceDb.collection('challenges').doc('hacky').set({ name: 'x' })));
  await check('7. authed: write user_challenges denied', () =>
    assertFails(aliceDb.collection('user_challenges').doc('alice_no-sugar').set({ userId: 'alice' })));
  await check('7. unauth: write challenges denied', () =>
    assertFails(anonDb.collection('challenges').doc('hacky').set({ name: 'x' })));

  // ---- sanity: nothing at all is client-readable ----
  await check('8. authed: list ai_history denied', () =>
    assertFails(aliceDb.collection('ai_history').get()));

  await env.cleanup();

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
}

main().catch((e) => { console.error('Test harness error:', e); process.exit(2); });