import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";

import { analysisId, installPeer, link, olderId } from "./fixtures.mjs";

async function downloadText(page, button) {
  const pending = page.waitForEvent("download");
  await page.getByRole("button", { name: button, exact: true }).click();
  const download = await pending;
  return { name: download.suggestedFilename(), bytes: await readFile(await download.path()) };
}

async function expectNoOverflow(page) {
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
}

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

test("unknown linked instrument is explicit and can be corrected", async ({ page }) => {
  const peer = await installPeer(page);
  await page.goto(link(analysisId, "UNKNOWN/USD"));
  await expect(page.getByRole("alert")).toContainText("Инструмента из ссылки нет");
  await page.getByRole("combobox", { name: "Инструмент анализа" }).selectOption("BTC/USD");
  await expect(page.getByRole("img")).toBeVisible();
  expect(peer.errors).toEqual([]);
});
