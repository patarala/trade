import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";

import { analysis, analysisId, installPeer, link, olderId } from "./fixtures.mjs";

async function downloadText(page, button) {
  const pending = page.waitForEvent("download");
  await page.getByRole("button", { name: button, exact: true }).click();
  const download = await pending;
  return { name: download.suggestedFilename(), bytes: await readFile(await download.path()) };
}

async function expectNoOverflow(page) {
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
}

test("leaving a restored analysis opens the experiments list", async ({ page }) => {
  const run = { id: 7, simulation_id: 3, status: "completed", current_step: 12, total_steps: 12,
    started_at: "2026-01-01T12:00:00Z", instrument_ids: [] };
  const responses = {
    "/api/auth/me": { id: 1, email: "trader@example.test", role: "user", is_active: true },
    "/api/assets": [], "/api/instruments": [], "/api/simulations": [],
    "/api/simulations/3": { id: 3, name: "Market" },
    "/api/simulations/3/regimex/run": run,
    "/api/simulations/3/regimex": { enabled: false, eligible: true, analysis: null, reason: null },
    "/api/simulations/3/results": { ...run, balances: [], initial_portfolio_value: "100",
      portfolio_value: "105", profit: "5", return_pct: "5", trade_count: 8, test_trade_count: 2 },
  };
  await page.addInitScript(() => localStorage.setItem("exchange-simulator-token", "browser-fixture-token"));
  await page.route("**/api/**", route => {
    const response = responses[new URL(route.request().url()).pathname];
    return response === undefined ? route.fulfill({ status: 404 }) : route.fulfill({ json: response });
  });
  await page.goto(`/#/simulations/3/runs/7/analyses/${analysisId}`);
  const back = page.getByRole("button", { name: "К экспериментам", exact: true });
  await expect(back).toBeVisible();
  await page.getByRole("button", { name: "Профиль", exact: true }).click();
  await expect(page.getByRole("heading", { name: "trader@example.test" })).toBeVisible();
  await page.goBack();
  await back.click();
  await expect(page.getByRole("heading", { name: "Мои эксперименты" })).toBeVisible();
});

test("a saved link still exports the run when Regimex is disabled", async ({ page }) => {
  await installPeer(page);
  await page.route("**/regimex?*", (route) => route.fulfill({
    json: { enabled: false, eligible: true, analysis: null, reason: null },
  }));
  await page.goto(link());
  await expect(page.getByRole("alert")).toContainText("Regimex сейчас отключён");
  await expect(page.getByRole("button", { name: "Анализировать" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Скачать отчёт" })).toHaveCount(0);
  const exportFile = await downloadText(page, "Скачать данные");
  expect(exportFile.name).toBe("regimex-export.json");
  expect(JSON.parse(exportFile.bytes.toString("utf8")).observations.length).toBeGreaterThan(0);
});

test("the ordinary monitor offers a local export with Regimex disabled or offline", async ({ page }) => {
  await installPeer(page);
  await page.setViewportSize({ width: 390, height: 844 });
  let disabled = true;
  await page.route("**/regimex?*", (route) => route.fulfill(disabled
    ? { json: { enabled: false, eligible: true, analysis: null, reason: null } }
    : { status: 503, json: { detail: "Offline", code: "analysis_unavailable", export_available: true } }));
  await page.goto("/tests/browser/index.html");
  await expect(page.getByRole("region", { name: "Данные запуска" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Анализировать" })).toHaveCount(0);
  disabled = false;
  await page.reload();
  await expect(page.getByRole("alert")).toBeVisible();
  const file = await downloadText(page, "Скачать данные");
  expect(JSON.parse(file.bytes.toString("utf8")).observations.length).toBeGreaterThan(0);
  await expect(page.getByRole("button", { name: "Проверить состояние" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Скачать отчёт" })).toHaveCount(0);
  await expectNoOverflow(page);
});

test("a first overview without local authorization cannot advertise downloads", async ({ page }) => {
  for (const status of [401, 403, 404, 503]) {
    await page.route("**/regimex?*", (route) => route.fulfill({ status, json: { detail: "Unavailable" } }));
    await page.goto("/tests/browser/index.html");
    await expect(page.getByRole("alert")).toBeVisible();
    await expect(page.getByRole("button", { name: "Скачать данные" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Скачать отчёт" })).toHaveCount(0);
    await page.unroute("**/regimex?*");
  }
});

test("saved links, linked charts, missing values, zoom and CSV work at capacity one", async ({ page }) => {
  const peer = await installPeer(page);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(link());
  await expect(page.getByRole("region", { name: "Классификация модели" })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Анализ", exact: true })).toHaveValue(analysisId);
  await page.getByRole("checkbox", { name: "Все инструменты" }).check();
  await expect(page.locator(".regimex-chart")).toHaveCount(2);
  const chart = page.getByRole("img", { name: "Цена и режимы BTC/USD, шаги 1–12" });
  await chart.focus();
  await chart.press("Home");
  await chart.press("ArrowRight");
  for (const point of await page.locator(".regimex-point").all()) await expect(point).toContainText("Шаг 2");
  await chart.press("ArrowRight");
  await chart.press("ArrowRight");
  await chart.press("ArrowRight");
  for (const point of await page.locator(".regimex-point").all()) {
    await expect(point).toContainText("Шаг 5");
    await expect(point).toContainText("Цена неизвестна");
  }
  const path = await page.locator(".regimex-price-line:not(.regimex-carried-line)").first().getAttribute("d");
  expect(path.match(/M/g).length).toBeGreaterThan(1);
  await page.getByRole("combobox", { name: "Отображение цены" }).selectOption("change");
  await expect(page.locator(".regimex-baseline")).toHaveCount(2);
  await page.getByRole("button", { name: "Показать шаги 5–8: Недостаточно данных" }).click();
  for (const image of await page.getByRole("img").all()) await expect(image).toHaveAccessibleName(/шаги 5–8/);
  const details = page.getByRole("region", { name: "Подробности интервала 5–8" });
  await expect(details).toContainText("Неполные данные");
  await expect(details).toContainText("на шагах 6 и 7");
  const csv = await downloadText(page, "Наблюдения CSV");
  expect([...csv.bytes.subarray(0, 3)]).toEqual([239, 187, 191]);
  expect(csv.name).toContain("steps-5-8.csv");
  const lines = csv.bytes.toString("utf8").trim().split("\r\n");
  expect(lines).toHaveLength(5);
  expect(lines[0]).toContain("price_state");
  expect(lines.some((line) => line.includes("unknown"))).toBe(true);
  expect(lines.some((line) => line.includes("carried"))).toBe(true);
  await page.getByRole("button", { name: "Весь запуск" }).click();
  await page.getByRole("combobox", { name: "Инструмент анализа" }).selectOption("ETH/USD");
  await expect(page).toHaveURL(/instrument=ETH%2FUSD/);
  const intervals = await downloadText(page, "Интервалы CSV");
  expect(intervals.bytes.toString("utf8")).toContain("ETH/USD");
  await page.setViewportSize({ width: 390, height: 844 });
  await expectNoOverflow(page);
  await page.reload();
  await expect(page.getByRole("combobox", { name: "Инструмент анализа" })).toHaveValue("ETH/USD");
  await page.getByRole("combobox", { name: "Анализ", exact: true }).selectOption(olderId);
  await expect(page.locator(".regimex-model")).toContainText("baseline-saved");
  await expect(page).toHaveURL(new RegExp(olderId));
  expect(peer.peak).toBe(1);
  expect(peer.errors).toEqual([]);
  expect(errors).toEqual([]);
});

test("failed preview can retry while report remains available without creating a job", async ({ page }) => {
  const peer = await installPeer(page, { previewFailures: 1 });
  await page.goto(link());
  await expect(page.getByRole("alert")).toContainText("Preview temporarily unavailable");
  const report = await downloadText(page, "Скачать отчёт");
  expect(report.bytes.toString("utf8")).toContain("Saved fixture report");
  await page.getByRole("button", { name: "Повторить загрузку" }).click();
  await expect(page.getByRole("region", { name: "Классификация модели" })).toBeVisible();
  expect(peer.previews).toBe(2);
  expect(peer.errors).toEqual([]);
});

test("a report that was not saved keeps the classification and local export available", async ({ page }) => {
  await installPeer(page);
  await page.route("**/analyses/*/report?*", (route) => route.fulfill({
    status: 404, json: { detail: "No HTML report was saved.", code: "report_unavailable" },
  }));
  await page.goto(link());
  const classification = page.getByRole("region", { name: "Классификация модели" });
  await expect(classification).toBeVisible();
  await page.getByRole("button", { name: "Скачать отчёт" }).click();
  await expect(page.getByText("HTML-отчёт для этого анализа не сохранён.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Скачать отчёт" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Проверить состояние" })).toHaveCount(0);
  await expect(classification).toBeVisible();
  const exported = await downloadText(page, "Скачать данные");
  expect(JSON.parse(exported.bytes.toString("utf8")).observations.length).toBeGreaterThan(0);
});

test("unknown linked instrument is explicit and can be corrected", async ({ page }) => {
  const peer = await installPeer(page);
  await page.goto(link(analysisId, "UNKNOWN/USD"));
  await expect(page.getByRole("alert")).toContainText("Инструмента из ссылки нет");
  await page.getByRole("combobox", { name: "Инструмент анализа" }).selectOption("BTC/USD");
  await expect(page.getByRole("img")).toBeVisible();
  expect(peer.errors).toEqual([]);
});

test("historical retry keeps its link until the exact submission is acknowledged", async ({ page }) => {
  const peer = await installPeer(page);
  const acceptedId = "00000000-0000-4000-8000-000000000003";
  const accepted = { ...analysis(acceptedId), state: "queued", result_available: false, report_available: false };
  const submissions = [];
  await page.route("**/regimex**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path.endsWith("/regimex")) {
      const selected = url.searchParams.get("analysis_id") === olderId
        ? { ...analysis(olderId), state: "failed", result_available: false, report_available: false } : accepted;
      return route.fulfill({ json: { enabled: true, eligible: true, reason: null, analysis: selected } });
    }
    if (route.request().method() === "POST" && path.endsWith("/analyses")) {
      submissions.push(route.request().postDataJSON());
      if (submissions.length === 1) return route.fulfill({ status: 503, headers: { "Retry-After": "2" },
        json: { code: "analysis_busy", detail: "Analysis capacity is busy", retryable: true } });
      if (submissions.length === 2) return route.abort("failed");
      return route.fulfill({ status: 202, json: accepted });
    }
    if (path.endsWith(`/analyses/${acceptedId}`)) return route.fulfill({ json: accepted });
    return route.fallback();
  });
  await page.goto(link(olderId));
  const retry = page.getByRole("button", { name: "Повторить анализ", exact: true });
  await retry.click();
  await expect(page.getByRole("alert")).toContainText("2 с.");
  await expect(page).toHaveURL(new RegExp(olderId));
  await retry.click();
  await expect(page.getByRole("alert")).toContainText("Подтверждение запуска не получено");
  await expect(page).toHaveURL(new RegExp(olderId));
  await page.getByRole("button", { name: "Повторить запрос запуска" }).click();
  await expect(page).toHaveURL(new RegExp(acceptedId));
  await expect(page.getByRole("combobox", { name: "Анализ", exact: true })).toHaveValue(acceptedId);
  expect(submissions).toEqual(Array.from({ length: 3 }, () => ({ retry_of: olderId })));
  expect(peer.errors).toEqual([]);
});

test("an older analysis opens inside the monitor when only one upstream read is available", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("exchange-simulator-token", "browser-fixture-token"));
  const scopedReads = [];
  const run = { id: 7, simulation_id: 3, status: "completed", current_step: 12, total_steps: 12,
    seed: 43, started_at: "2026-01-01T12:00:00Z", instrument_ids: [], error_message: null };
  const replies = {
    "/api/auth/me": { id: 1, email: "trader@example.test", role: "user", is_active: true },
    "/api/assets": [], "/api/instruments": [],
    "/api/simulations/3": { id: 3, name: "Тестовый эксперимент" },
    "/api/simulations/3/regimex/run": run, "/api/simulations/3/balances": [],
    "/api/simulations/3/results": { ...run, balances: [], initial_portfolio_value: "100",
      portfolio_value: "105", profit: "5", return_pct: "5", trade_count: 8, test_trade_count: 2 },
  };
  const selected = { ...analysis(olderId), state: "failed", failure: { code: "source_failure",
    message: "The analysis source was temporarily unavailable.", retryable: true }, result_available: false, report_available: false };
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (Object.hasOwn(replies, url.pathname)) return route.fulfill({ json: replies[url.pathname] });
    if (!url.pathname.startsWith("/api/simulations/3/regimex")) return route.fulfill({ status: 404 });
    scopedReads.push(url);
    if (scopedReads.length > 1) return route.fulfill({ status: 429, headers: { "Retry-After": "60" },
      json: { code: "analysis_busy", detail: "Too many requests" } });
    return route.fulfill({ json: { enabled: true, eligible: true, reason: null,
      analysis: url.searchParams.get("analysis_id") === olderId ? selected : analysis(analysisId) } });
  });
  await page.goto("/" + new URL(link(olderId), "http://localhost").hash);
  const monitor = page.getByRole("region", { name: "Состояние запуска" });
  const panel = page.getByRole("region", { name: "Анализ Regimex" });
  await expect(monitor).toContainText("Завершён");
  await expect(panel.getByRole("button", { name: "Повторить анализ" })).toBeEnabled();
  await expect(panel).toContainText("Источник данных временно недоступен");
  await expect(panel).toContainText("Повторите через 60 с.");
  expect(scopedReads[0].searchParams.get("analysis_id")).toBe(olderId);
  expect(scopedReads.some((url) => url.pathname.endsWith(`/analyses/${olderId}`))).toBe(false);
  await expectNoOverflow(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await expectNoOverflow(page);
  await expect(panel.getByRole("button", { name: "Обновить историю" })).toBeEnabled();
});
