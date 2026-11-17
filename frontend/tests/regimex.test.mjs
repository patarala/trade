import assert from "node:assert/strict";
import test, { after } from "node:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import ts from "typescript";
import { previewPayloads } from "./regimex-fixtures.mjs";

async function moduleSource(name) {
  const source = await readFile(new URL(`../src/${name}.ts`, import.meta.url), "utf8");
  return ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2023 },
  }).outputText;
}
const directory = await mkdtemp(join(tmpdir(), "trade-regimex-tests-"));
after(() => rm(directory, { recursive: true, force: true }));
await writeFile(join(directory, "regimexResult.mjs"), await moduleSource("regimexResult"));
await writeFile(join(directory, "regimexApi.mjs"),
  (await moduleSource("regimexApi")).replaceAll('"./regimexResult"', '"./regimexResult.mjs"'));
await writeFile(join(directory, "regimexController.mjs"),
  (await moduleSource("regimexController")).replaceAll('"./regimexApi"', '"./regimexApi.mjs"'));
const { RegimexApi, RegimexRequestError } = await import(pathToFileURL(join(directory, "regimexApi.mjs")));
const { RegimexController } = await import(pathToFileURL(join(directory, "regimexController.mjs")));

const firstId = "00000000-0000-4000-8000-000000000001";
const secondId = "00000000-0000-4000-8000-000000000002";
const info = (state = "queued", id = firstId) => ({
  analysis_id: id, run_id: 7, state, stage: null, failure: null,
  model_version_id: "tcn-test", created_at: "2026-01-01T12:00:00+00:00",
  result_available: state === "succeeded", report_available: state === "succeeded",
});
const status = (analysis = null) => ({ enabled: true, eligible: true, reason: null, analysis });
const settled = () => new Promise((resolve) => setImmediate(resolve));
const preview = (analysisId = firstId) => ({ analysisId, modelVersion: "tcn-preview", instruments: [] });

function deferred() {
  let resolve, reject;
  const promise = new Promise((accept, decline) => { resolve = accept; reject = decline; });
  return { promise, resolve, reject };
}

function panel(overrides = {}, analysisId = null) {
  const timers = [];
  const downloads = [];
  const client = {
    status: async () => status(), poll: async () => info("running"),
    submit: async () => info(), download: async () => new Blob(["report"]),
    preview: async (id) => preview(id),
    ...overrides,
  };
  const controller = new RegimexController(client,
    (blob, name) => downloads.push({ blob, name }),
    (callback, delay) => {
      const timer = { callback, delay, cancelled: false };
      timers.push(timer);
      return () => { timer.cancelled = true; };
    }, analysisId);
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

test("a saved older analysis stays selected during refresh and report downloads", async () => {
  const polled = [], downloaded = [];
  const p = panel({
    status: async () => status(info("succeeded", secondId)),
    poll: async (id) => { polled.push(id); return info("succeeded", id); },
    download: async (_kind, id) => { downloaded.push(id); return new Blob(["saved report"]); },
  }, firstId);
  p.controller.start();
  await settled();
  assert.equal(p.controller.getSnapshot().preview.analysisId, firstId);
  await p.controller.refresh();
  await p.controller.download("report");
  assert.deepEqual(polled, [firstId, firstId]);
  assert.deepEqual(downloaded, [firstId]);
  p.controller.stop();
});

test("an unavailable linked analysis fails explicitly without showing the latest result", async () => {
  const p = panel({
    status: async () => status(info("succeeded", secondId)),
    poll: async () => { throw new RegimexRequestError("Analysis not found", 404); },
  }, firstId);
  p.controller.start();
  await settled();
  assert.equal(p.controller.getSnapshot().status, null);
  assert.equal(p.controller.getSnapshot().preview, null);
  assert.equal(p.controller.getSnapshot().error, "Analysis not found");
  assert.equal(p.next(), undefined);
  p.controller.stop();
});

test("leaving a saved analysis aborts its outstanding identity check", async () => {
  const delayed = deferred();
  let signal;
  const p = panel({
    status: async () => status(info("succeeded", secondId)),
    poll: async (_id, value) => { signal = value; return delayed.promise; },
  }, firstId);
  p.controller.start();
  await settled();
  p.controller.stop();
  delayed.resolve(info("succeeded", firstId));
  await settled();
  assert.equal(signal.aborted, true);
  assert.equal(p.controller.getSnapshot().status, null);
});

test("a lost initial acknowledgement can be recovered with the same submission", async () => {
  const submissions = [];
  const p = panel({
    submit: async (retryOf) => {
      submissions.push(retryOf);
      if (submissions.length === 1) throw new TypeError("network lost");
      return info();
    },
  });
  p.controller.start();
  await settled();
  await p.controller.submit();
  assert.equal(p.controller.getSnapshot().confirmationRequired, true);
  await p.controller.submit();
  assert.deepEqual(submissions, [null, null]);
  assert.equal(p.controller.getSnapshot().status.analysis.analysis_id, firstId);
  assert.equal(p.controller.getSnapshot().confirmationRequired, false);
  assert.equal(p.controller.getSnapshot().error, "");
  assert.ok(p.next());
  p.controller.stop();
});

test("a rejected historical retry stays selected; an uncertain retry resolves only by its own key", async () => {
  const acceptedId = "00000000-0000-4000-8000-000000000003";
  const busy = new RegimexRequestError("Retry after 2 s", 503, 2, "analysis_busy");
  const submissions = [];
  let overviewReads = 0;
  const p = panel({
    status: async () => {
      overviewReads += 1;
      return status(info("succeeded", secondId));
    },
    poll: async (id) => info("failed", id),
    submit: async (retryOf) => {
      submissions.push(retryOf);
      if ([1, 3].includes(submissions.length)) throw busy;
      if (submissions.length === 2) throw new RegimexRequestError("Gateway timed out", 504);
      return info("queued", acceptedId);
    },
  }, firstId);
  p.controller.start();
  await settled();
  await p.controller.submit();
  assert.equal(p.controller.getSnapshot().status.analysis.analysis_id, firstId);
  assert.equal(p.controller.getSnapshot().error, busy.message);
  assert.equal(p.controller.getSnapshot().confirmationRequired, false);
  await p.controller.submit();
  await p.controller.refresh();
  assert.equal(p.controller.getSnapshot().status.analysis.analysis_id, firstId);
  assert.equal(p.controller.getSnapshot().confirmationRequired, true);
  assert.equal(overviewReads, 1);
  await p.controller.submit();
  assert.equal(p.controller.getSnapshot().confirmationRequired, true);
  await p.controller.submit();
  assert.deepEqual(submissions, [firstId, firstId, firstId, firstId]);
  assert.equal(p.controller.getSnapshot().confirmationRequired, false);
  assert.equal(p.controller.getSnapshot().status.analysis.analysis_id, acceptedId);
  assert.equal(p.controller.getSnapshot().error, "");
  p.controller.stop();
});

test("definitive status failures clear the selected result and cancel its download", async (t) => {
  for (const code of [401, 403, 404]) await t.test(String(code), async () => {
    let allowed = true;
    const blob = deferred();
    const p = panel({
      status: async () => status(info("succeeded", secondId)),
      poll: async (id) => {
        if (!allowed) throw new RegimexRequestError("Unavailable", code);
        return info("succeeded", id);
      },
      download: () => blob.promise,
    }, firstId);
    p.controller.start();
    await settled();
    assert.equal(p.controller.getSnapshot().preview.analysisId, firstId);
    const download = p.controller.download("report");
    allowed = false;
    await p.controller.refresh();
    assert.equal(p.controller.getSnapshot().preview, null);
    assert.equal(p.controller.getSnapshot().status, null);
    assert.equal(p.controller.getSnapshot().downloading, null);
    blob.resolve(new Blob(["stale report"]));
    await download;
    assert.deepEqual(p.downloads, []);
    allowed = true;
    await p.controller.refresh();
    assert.equal(p.controller.getSnapshot().status.analysis.analysis_id, firstId);
    p.controller.stop();
  });
});

test("a replay that loses access releases the confirmation state for an access check", async () => {
  let attempts = 0;
  const p = panel({
    status: async () => status(info("failed", firstId)),
    submit: async () => {
      if (++attempts === 1) throw new TypeError("network lost");
      throw new RegimexRequestError("Analysis no longer available", 404);
    },
  }, firstId);
  p.controller.start();
  await settled();
  await p.controller.submit();
  await p.controller.submit();
  const denied = p.controller.getSnapshot();
  assert.equal(denied.status, null);
  assert.equal(denied.submitting, false);
  assert.equal(denied.confirmationRequired, false);
  await p.controller.refresh();
  assert.equal(p.controller.getSnapshot().status.analysis.analysis_id, firstId);
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

test("history pages are bounded and every item must belong to this run", async (t) => {
  const calls = [];
  let page = { items: [info("succeeded")], limit: 10, offset: 20, has_more: false };
  t.mock.method(globalThis, "fetch", async (url) => { calls.push(url); return Response.json(page); });
  const api = new RegimexApi(3, 7, "user-jwt"), signal = new AbortController().signal;
  const result = await api.history(20, signal);
  assert.equal(result.items[0].model_version_id, "tcn-test");
  assert.equal(calls[0], "/api/simulations/3/regimex/analyses?limit=10&offset=20&run_id=7");
  for (const offset of [-1, 10001, 0.5]) await assert.rejects(api.history(offset, signal));
  assert.equal(calls.length, 1);
  for (const invalid of [
    { offset: 0 }, { limit: 11 }, { has_more: true },
    { items: [info(), info()] }, { items: [{ ...info(), run_id: 999 }] },
    { items: [{ ...info(), created_at: "not a date" }] },
    { items: [{ ...info(), model_version_id: "" }] },
  ]) {
    page = { items: [info()], limit: 10, offset: 20, has_more: false, ...invalid };
    await assert.rejects(api.history(20, signal), RegimexRequestError);
  }
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

test("classification loads only after success and stays cached across status refreshes", async () => {
  let completed = false;
  const ids = [];
  const p = panel({
    status: async () => status(info(completed ? "succeeded" : "running")),
    poll: async () => { completed = true; return info("succeeded"); },
    preview: async (id) => { ids.push(id); return preview(id); },
  });
  p.controller.start();
  await settled();
  assert.deepEqual(ids, []);
  assert.equal(p.controller.getSnapshot().previewLoading, false);
  await p.tick();
  assert.deepEqual(ids, [firstId]);
  assert.equal(p.controller.getSnapshot().preview.analysisId, firstId);
  await p.controller.refresh();
  await p.controller.refreshPreview();
  assert.deepEqual(ids, [firstId]);
  p.controller.stop();
});

test("preview errors are independent of reports and retry only on explicit preview retry", async () => {
  let attempts = 0, submissions = 0;
  const p = panel({
    status: async () => status(info("succeeded")),
    submit: async () => { submissions += 1; return info(); },
    preview: async (id) => {
      attempts += 1;
      if (attempts === 1) throw new RegimexRequestError("Classification unavailable", 503);
      return preview(id);
    },
  });
  p.controller.start();
  await settled();
  assert.equal(p.controller.getSnapshot().previewError, "Classification unavailable");
  assert.equal(p.controller.getSnapshot().error, "");
  assert.equal(p.controller.getSnapshot().previewLoading, false);
  await p.controller.refresh();
  assert.equal(attempts, 1);
  await p.controller.download("report");
  assert.equal(p.downloads.length, 1);
  assert.equal(p.controller.getSnapshot().previewError, "Classification unavailable");
  await p.controller.refreshPreview();
  assert.equal(attempts, 2);
  assert.equal(submissions, 0);
  assert.equal(p.controller.getSnapshot().previewError, null);
  assert.equal(p.controller.getSnapshot().preview.analysisId, firstId);
  p.controller.stop();
});

test("changing the analysis aborts old preview work and ignores late success or failure", async (t) => {
  for (const failure of [false, true]) await t.test(failure ? "late failure" : "late success", async () => {
    const delayed = deferred();
    let latest = firstId, oldSignal;
    const p = panel({
      status: async () => status(info("succeeded", latest)),
      preview: async (id, signal) => {
        if (id === firstId) { oldSignal = signal; return delayed.promise; }
        return preview(id);
      },
    });
    p.controller.start();
    await settled();
    assert.equal(p.controller.getSnapshot().previewLoading, true);
    latest = secondId;
    await p.controller.refresh();
    await settled();
    assert.equal(oldSignal.aborted, true);
    if (failure) delayed.reject(new RegimexRequestError("Old failure", 503));
    else delayed.resolve(preview(firstId));
    await settled();
    assert.equal(p.controller.getSnapshot().preview.analysisId, secondId);
    assert.equal(p.controller.getSnapshot().previewError, null);
    assert.equal(p.controller.getSnapshot().previewLoading, false);
    p.controller.stop();
  });
});

test("disabling integration or leaving a run removes and fences its classification", async (t) => {
  for (const change of ["disable", "stop"]) await t.test(change, async () => {
    const delayed = deferred();
    let enabled = true, signal;
    const p = panel({
      status: async () => ({ ...status(info("succeeded")), enabled }),
      preview: async (_id, value) => { signal = value; return delayed.promise; },
    });
    p.controller.start();
    await settled();
    if (change === "stop") p.controller.stop();
    else { enabled = false; await p.controller.refresh(); }
    delayed.resolve(preview());
    await settled();
    assert.equal(signal.aborted, true);
    assert.equal(p.controller.getSnapshot().preview, null);
    assert.equal(p.controller.getSnapshot().previewError, null);
    p.controller.stop();
  });
});

test("restart of the same controller fences the previous lifetime's preview", async () => {
  const delayed = deferred();
  let attempts = 0;
  const p = panel({
    status: async () => status(info("succeeded")),
    preview: async (id) => {
      attempts += 1;
      return attempts === 1 ? delayed.promise : { ...preview(id), modelVersion: "current" };
    },
  });
  p.controller.start();
  await settled();
  p.controller.stop();
  p.controller.start();
  await settled();
  delayed.resolve({ ...preview(), modelVersion: "old" });
  await settled();
  assert.equal(attempts, 2);
  assert.equal(p.controller.getSnapshot().preview.modelVersion, "current");
  p.controller.stop();
});

test("preview API accepts full-size native runs and fetches only bound JSON endpoints", async (t) => {
  const payloads = previewPayloads(4096);
  assert.ok(JSON.stringify(payloads.input).length > 128 * 1024);
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    calls.push({ url, options });
    return Response.json(payloads);
  });
  const result = await new RegimexApi(3, 7, "user-jwt").preview(firstId, new AbortController().signal);
  assert.equal(result.instruments.length, 2);
  assert.equal(result.instruments[1].steps.length, 4096);
  assert.deepEqual(calls.map((call) => call.url).sort(), [
    `/api/simulations/3/regimex/analyses/${firstId}/preview?run_id=7`,
  ].sort());
  for (const { options } of calls) {
    assert.equal(options.method, "GET");
    assert.equal(options.headers.get("Authorization"), "Bearer user-jwt");
    assert.equal(options.redirect, "error");
    assert.equal(options.mode, "same-origin");
  }
});

test("preview API refuses mismatched content and never falls back to an HTML response", async (t) => {
  const payloads = previewPayloads();
  payloads.result.snapshot_sha256 = "f".repeat(64);
  t.mock.method(globalThis, "fetch", async () => Response.json(payloads));
  const api = new RegimexApi(3, 7, "user-jwt");
  await assert.rejects(api.preview(firstId, new AbortController().signal), RegimexRequestError);
  globalThis.fetch = async () => new Response("<h1>Unexpected report</h1>", { headers: { "Content-Type": "text/html" } });
  await assert.rejects(api.preview(firstId, new AbortController().signal), RegimexRequestError);
});

test("combined preview has a 16 MiB streaming cap and cancels an oversized body", async (t) => {
  let cancelled = false;
  t.mock.method(globalThis, "fetch", async () => new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(new Uint8Array(16 * 1024 * 1024));
      controller.enqueue(new Uint8Array(1));
    },
    cancel() { cancelled = true; },
  }), { headers: { "Content-Type": "application/json" } }));
  const api = new RegimexApi(3, 7, "user-jwt");
  await assert.rejects(api.preview(firstId, new AbortController().signal), RegimexRequestError);
  assert.equal(cancelled, true);
});

test("preview abort is honored even when a transport supplies a late complete response", async (t) => {
  const payloads = previewPayloads(), delayed = deferred();
  t.mock.method(globalThis, "fetch", async (url) => {
    await delayed.promise;
    return Response.json(payloads);
  });
  const controller = new AbortController();
  const pending = new RegimexApi(3, 7, "user-jwt").preview(firstId, controller.signal);
  controller.abort();
  delayed.resolve();
  await assert.rejects(pending, { name: "AbortError" });
});


test("one API serializes overview, history, preview and downloads through body completion", async (t) => {
  const payloads = previewPayloads(), held = deferred(), firstRead = deferred();
  let active = 0, peak = 0;
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url) => {
    active += 1;
    peak = Math.max(peak, active);
    calls.push(url);
    const payload = url.includes("/preview?") ? payloads : url.includes("limit=")
      ? { items: [info("succeeded")], limit: 10, offset: 0, has_more: false } : status(info("succeeded"));
    return new Response(new ReadableStream({
      async start(controller) {
        if (calls.length === 1) { firstRead.resolve(); await held.promise; }
        controller.enqueue(new TextEncoder().encode(JSON.stringify(payload)));
        active -= 1;
        controller.close();
      },
    }), { headers: { "Content-Type": "application/json" } });
  });
  const api = new RegimexApi(3, 7, "jwt"), signal = new AbortController().signal;
  const requests = [api.status(signal), api.history(0, signal), api.preview(firstId, signal), api.download("export", null, signal)];
  await firstRead.promise;
  await settled();
  assert.equal(calls.length, 1, "headers alone must not release the request slot");
  held.resolve();
  const values = await Promise.all(requests);
  assert.equal(peak, 1);
  assert.equal(values[2].instruments.length, 2);
  assert.equal(calls.length, 4);
});

test("aborting a queued history request neither sends it nor overtakes the active request", async (t) => {
  const held = deferred();
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url) => {
    calls.push(url);
    if (calls.length === 1) await held.promise;
    return Response.json(status());
  });
  const api = new RegimexApi(3, 7, "jwt"), lifetime = new AbortController(), cancelled = new AbortController();
  const first = api.status(lifetime.signal);
  await settled();
  const second = api.history(0, cancelled.signal);
  cancelled.abort();
  const third = api.status(lifetime.signal);
  await assert.rejects(second, { name: "AbortError" });
  await settled();
  assert.equal(calls.length, 1);
  held.resolve();
  await Promise.all([first, third]);
  assert.equal(calls.length, 2);
});

test("model identifiers follow the shared Unicode code-point contract", async (t) => {
  const cases = JSON.parse(await readFile(new URL("../../tests/contracts/identifiers.json", import.meta.url), "utf8"));
  for (const fixture of cases) {
    const value = info("succeeded");
    value.model_version_id = fixture.value;
    t.mock.method(globalThis, "fetch", async () => Response.json(status(value)));
    const pending = new RegimexApi(3, 7, "jwt").status(new AbortController().signal);
    if (fixture.valid) assert.equal((await pending).analysis.model_version_id, fixture.value, fixture.name);
    else await assert.rejects(pending, RegimexRequestError, fixture.name);
  }
});


test("capacity rejection keeps Retry-After visible after an aborted server operation", async (t) => {
  t.mock.method(globalThis, "fetch", async () => Response.json(
    { detail: "Analysis capacity is busy", code: "analysis_busy", retryable: true },
    { status: 503, headers: { "Retry-After": "2" } },
  ));
  await assert.rejects(new RegimexApi(3, 7, "jwt").history(0, new AbortController().signal),
    (error) => error.status === 503 && error.code === "analysis_busy" && error.retryAfter === 2
      && error.message.includes("2 с."));
});
