import assert from "node:assert/strict";
import test, { after } from "node:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import ts from "typescript";

async function moduleSource(name) {
  const source = await readFile(new URL(`../src/${name}.ts`, import.meta.url), "utf8");
  return ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2023 },
  }).outputText;
}
const directory = await mkdtemp(join(tmpdir(), "trade-regimex-tests-"));
after(() => rm(directory, { recursive: true, force: true }));
await writeFile(join(directory, "regimexApi.mjs"), await moduleSource("regimexApi"));
await writeFile(join(directory, "regimexController.mjs"),
  (await moduleSource("regimexController")).replaceAll('"./regimexApi"', '"./regimexApi.mjs"'));
const { RegimexApi, RegimexRequestError } = await import(pathToFileURL(join(directory, "regimexApi.mjs")));
const { RegimexController } = await import(pathToFileURL(join(directory, "regimexController.mjs")));

const firstId = "00000000-0000-4000-8000-000000000001";
const secondId = "00000000-0000-4000-8000-000000000002";
const info = (state = "queued", id = firstId) => ({
  analysis_id: id, run_id: 7, state, stage: null, failure: null,
  result_available: state === "succeeded", report_available: state === "succeeded",
});
const status = (analysis = null) => ({ enabled: true, eligible: true, reason: null, analysis });
const settled = () => new Promise((resolve) => setImmediate(resolve));

function deferred() {
  let resolve, reject;
  const promise = new Promise((accept, decline) => { resolve = accept; reject = decline; });
  return { promise, resolve, reject };
}

function panel(overrides = {}) {
  const timers = [];
  const downloads = [];
  const client = {
    status: async () => status(), poll: async () => info("running"),
    submit: async () => info(), download: async () => new Blob(["report"]),
    ...overrides,
  };
  const controller = new RegimexController(client,
    (blob, name) => downloads.push({ blob, name }),
    (callback, delay) => {
      const timer = { callback, delay, cancelled: false };
      timers.push(timer);
      return () => { timer.cancelled = true; };
    });
  const next = () => timers.find((timer) => !timer.cancelled);
  return {
    controller, downloads, timers, next,
    async tick() {
      const timer = next();
      assert.ok(timer, "a poll should be scheduled");
      timer.cancelled = true;
      timer.callback();
      await settled();
    },
  };
}

test("disabled integration performs no submission or polling", async () => {
  let submissions = 0;
  const p = panel({
    status: async () => ({ ...status(), enabled: false }),
    submit: async () => { submissions += 1; return info(); },
  });
  p.controller.start();
  await settled();
  await p.controller.submit();
  assert.equal(p.controller.getSnapshot().status.enabled, false);
  assert.equal(submissions, 0);
  assert.equal(p.next(), undefined);
  p.controller.stop();
});

test("reopening completed analysis restores report without submitting another job", async () => {
  let submissions = 0;
  const p = panel({
    status: async () => status(info("succeeded")),
    submit: async () => { submissions += 1; return info(); },
  });
  p.controller.start();
  await settled();
  await p.controller.submit();
  await p.controller.download("report");
  assert.equal(submissions, 0);
  assert.equal(p.downloads[0].name, `regimex-${firstId}.html`);
  assert.equal(p.next(), undefined);
  p.controller.stop();
});

test("lost submission response is recovered from server state before retry", async () => {
  let accepted = false;
  let submissions = 0;
  const p = panel({
    status: async () => status(accepted ? info() : null),
    submit: async () => { accepted = true; submissions += 1; throw new TypeError("network lost"); },
  });
  p.controller.start();
  await settled();
  await p.controller.submit();
  await p.controller.submit();
  assert.equal(submissions, 1);
  assert.equal(p.controller.getSnapshot().status.analysis.analysis_id, firstId);
  assert.equal(p.controller.getSnapshot().confirmationRequired, false);
  assert.equal(p.controller.getSnapshot().error, "");
  assert.ok(p.next());
  p.controller.stop();
});

test("uncertain submission cannot be repeated while its status is unavailable", async () => {
  let initial = true;
  let recovered = false;
  let submissions = 0;
  const p = panel({
    status: async () => {
      if (initial) { initial = false; return status(); }
      if (recovered) return status(info());
      throw new RegimexRequestError("Temporarily unavailable", 503);
    },
    submit: async () => { submissions += 1; throw new TypeError("network lost"); },
  });
  p.controller.start();
  await settled();
  await p.controller.submit();
  assert.equal(p.controller.getSnapshot().confirmationRequired, true);
  await p.controller.submit();
  assert.equal(submissions, 1);
  recovered = true;
  await p.controller.refresh();
  assert.equal(p.controller.getSnapshot().confirmationRequired, false);
  assert.equal(p.controller.getSnapshot().status.analysis.state, "queued");
  p.controller.stop();
});

test("explicit retry identifies the failed analysis", async () => {
  const retries = [];
  const p = panel({
    status: async () => status(info("failed")),
    submit: async (retryOf) => { retries.push(retryOf); return info("queued", secondId); },
  });
  p.controller.start();
  await settled();
  await p.controller.submit();
  assert.deepEqual(retries, [firstId]);
  assert.equal(p.controller.getSnapshot().status.analysis.analysis_id, secondId);
  p.controller.stop();
});

test("leaving a run aborts requests and ignores late responses", async () => {
  const delayed = deferred();
  let signal;
  const p = panel({ status: async (s) => { signal = s; return delayed.promise; } });
  p.controller.start();
  p.controller.stop();
  delayed.resolve(status(info("succeeded")));
  await settled();
  assert.equal(signal.aborted, true);
  assert.equal(p.controller.getSnapshot().status, null);
  assert.equal(p.next(), undefined);
});

test("late poll cannot replace a newer manual refresh", async () => {
  const delayed = deferred();
  let completed = false;
  const p = panel({
    status: async () => status(info(completed ? "succeeded" : "queued")),
    poll: async () => delayed.promise,
  });
  p.controller.start();
  await settled();
  await p.tick();
  completed = true;
  await p.controller.refresh();
  delayed.resolve(info("running"));
  await settled();
  assert.equal(p.controller.getSnapshot().status.analysis.state, "succeeded");
  assert.equal(p.next(), undefined);
  p.controller.stop();
});

test("rate-limited polling stops until an explicit refresh", async () => {
  let polls = 0;
  const p = panel({
    status: async () => status(info()),
    poll: async () => { polls += 1; throw new RegimexRequestError("Wait 30 seconds", 429, 30); },
  });
  p.controller.start();
  await settled();
  await p.tick();
  assert.equal(polls, 1);
  assert.equal(p.next(), undefined);
  assert.equal(p.controller.getSnapshot().pollingPaused, true);
  assert.equal(p.controller.getSnapshot().error, "Wait 30 seconds");
  p.controller.stop();
});

test("continuous polling backs off and eventually pauses", async () => {
  const p = panel({ status: async () => status(info()) });
  p.controller.start();
  await settled();
  for (let i = 0; p.next() && i < 100; i += 1) await p.tick();
  assert.equal(p.next(), undefined);
  assert.equal(p.controller.getSnapshot().pollingPaused, true);
  assert.ok(p.timers[1].delay > p.timers[0].delay);
  assert.ok(p.timers.every((timer) => timer.delay >= 2000 && timer.delay <= 15_000));
  p.controller.stop();
});

test("late download cannot save a previous session's report", async () => {
  const delayed = deferred();
  let signal;
  const p = panel({
    status: async () => status(info("succeeded")),
    download: async (_kind, _id, s) => { signal = s; return delayed.promise; },
  });
  p.controller.start();
  await settled();
  const download = p.controller.download("report");
  p.controller.stop();
  delayed.resolve(new Blob(["old report"]));
  await download;
  assert.equal(signal.aborted, true);
  assert.deepEqual(p.downloads, []);
});

test("API keeps user credentials in headers and uses only local run-bound paths", async (t) => {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    calls.push({ url, options });
    return Response.json(info());
  });
  const api = new RegimexApi(3, 7, "user-jwt");
  await api.submit(null, new AbortController().signal);
  assert.equal(calls[0].url, "/api/simulations/3/regimex/analyses?run_id=7");
  assert.equal(calls[0].options.headers.get("Authorization"), "Bearer user-jwt");
  assert.equal(calls[0].options.headers.get("X-Regimex-Subject"), null);
  assert.equal(calls[0].options.redirect, "error");
  assert.equal(calls[0].options.mode, "same-origin");
  assert.deepEqual(JSON.parse(calls[0].options.body), {});
  await assert.rejects(api.poll("https://example.com/steal", new AbortController().signal));
  assert.equal(calls.length, 1);
});

test("API rejects analysis from another run or unexpected analysis ID", async (t) => {
  t.mock.method(globalThis, "fetch", async () => Response.json({ ...info(), run_id: 8 }));
  const api = new RegimexApi(3, 7, "user-jwt");
  await assert.rejects(api.poll(firstId, new AbortController().signal), RegimexRequestError);
  globalThis.fetch = async () => Response.json(info("running", secondId));
  await assert.rejects(api.poll(firstId, new AbortController().signal), RegimexRequestError);
});

test("API preserves safe rate-limit errors and refuses oversized responses", async (t) => {
  t.mock.method(globalThis, "fetch", async () => Response.json(
    { detail: "Too many requests", code: "rate_limit" }, { status: 429, headers: { "Retry-After": "30" } },
  ));
  const api = new RegimexApi(3, 7, "user-jwt");
  await assert.rejects(api.status(new AbortController().signal), (error) => {
    assert.equal(error.status, 429);
    assert.equal(error.retryAfter, 30);
    return true;
  });
  globalThis.fetch = async () => Response.json(status(), { headers: { "Content-Length": "999999999" } });
  await assert.rejects(api.status(new AbortController().signal), RegimexRequestError);
});

test("report download verifies media type and streams within the size limit", async (t) => {
  t.mock.method(globalThis, "fetch", async () => Response.json({ error: "not a report" }));
  const api = new RegimexApi(3, 7, "user-jwt");
  await assert.rejects(api.download("report", firstId, new AbortController().signal), RegimexRequestError);
  let cancelled = false;
  globalThis.fetch = async () => new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(new Uint8Array(16 * 1024 * 1024));
      controller.enqueue(new Uint8Array(1));
    },
    cancel() { cancelled = true; },
  }), { headers: { "Content-Type": "text/html" } });
  await assert.rejects(api.download("report", firstId, new AbortController().signal), RegimexRequestError);
  assert.equal(cancelled, true);
});
