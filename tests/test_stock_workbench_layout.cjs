"use strict";
// A local browser layout regression: no server, market data, or external fetch.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");
const test = require("node:test");
const root = path.resolve(__dirname, "..");

function browserLibrary() {
    try { return require("playwright"); } catch (_) { /* existing local cache */ }
    const cache = path.join(process.env.LOCALAPPDATA, "npm-cache", "_npx");
    if (fs.existsSync(cache)) for (const folder of fs.readdirSync(cache)) {
        const pkg = path.join(cache, folder, "node_modules/playwright/package.json");
        if (fs.existsSync(pkg)) return createRequire(pkg)("playwright");
    }
    throw new Error("Local Playwright required for layout regression; no downloads performed.");
}

test("narrow stock modal lets an ordinary wheel gesture reach the K-line", async () => {
    const { chromium } = browserLibrary();
    const executablePath = [chromium.executablePath(), path.join(process.env.ProgramFiles || "C:/Program Files", "Google/Chrome/Application/chrome.exe"),
        path.join(process.env["ProgramFiles(x86)"] || "C:/Program Files (x86)", "Microsoft/Edge/Application/msedge.exe")].find(p => fs.existsSync(p));
    assert.ok(executablePath, "existing local Chromium required");
    const template = fs.readFileSync(path.join(root, "app/templates/daily_research.html"), "utf8");
    const base = template.match(/<style>([\s\S]*?)<\/style>/)[1];
    const stock = fs.readFileSync(path.join(root, "app/static/css/stock_workbench.css"), "utf8");
    const browser = await chromium.launch({ executablePath, headless: true });
    try {
        const context = await browser.newContext();
        await context.route("**/*", route => route.abort());
        const page = await context.newPage();
        for (const [width, height] of [[421, 552], [390, 844]]) {
            await page.setViewportSize({ width, height });
            await page.setContent(`<style>${base}\n${stock}</style><body class="modal-open"><div id="detailModal" class="modal open"><article class="modal-content"><header class="modal-header">股票详情</header><div id="modalBody" class="modal-body"><div class="detail-shell"><aside class="detail-nav">看盘</aside><div class="detail-panel-area"><div class="detail-pane"><div class="detail-pane-grid"><section class="stock-quote-workbench" style="height:1400px">真实布局测试占位：报价与参考区</section><section id="layoutChart" style="height:330px">K线</section><section style="height:900px">研究简报</section></div></div></div></div></div></article></div></body>`);
            const body = page.locator("#modalBody");
            const before = await body.evaluate(n => ({ height: n.clientHeight, full: n.scrollHeight }));
            assert.ok(before.full > before.height + 1000, `content must remain scrollable at ${width}`);
            await body.hover();
            let reached = false;
            for (let step = 0; step < 12; step++) {
                await page.mouse.wheel(0, 200);
                await page.waitForTimeout(35);
                reached = await page.locator("#layoutChart").evaluate(n => {
                    const r = n.getBoundingClientRect(), box = document.getElementById("modalBody").getBoundingClientRect();
                    return r.top < box.bottom - 20 && r.bottom > box.top + 20;
                });
                if (reached) break;
            }
            assert.ok(reached, `wheel must expose K-line at ${width}x${height}`);
        }
        await context.close();
    } finally { await browser.close(); }
});
