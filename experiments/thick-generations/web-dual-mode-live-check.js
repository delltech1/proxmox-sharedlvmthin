#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-only

const { chromium } = require("playwright");

function required(name) {
    const value = process.env[name];
    if (!value) {
        throw new Error(`${name} is required`);
    }
    return value;
}

(async () => {
    const baseUrl = required("SLT_WEB_URL");
    const username = required("SLT_WEB_USER");
    const password = required("SLT_WEB_PASSWORD");
    const browser = await chromium.launch({ channel: "msedge", headless: true });
    try {
        const context = await browser.newContext({ ignoreHTTPSErrors: true });
        const page = await context.newPage();
        await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
        await page.locator('input[name="username"]').fill(username);
        await page.locator('input[name="password"]').fill(password);
        await Promise.all([
            page.waitForURL(url => !url.pathname.endsWith("/login")),
            page.locator('button[type="submit"], form button').click(),
        ]);
        await page.locator("#api").filter({ hasText: "API healthy" }).waitFor();
        const capacityUnits = await page.evaluate(() => [
            cap(1024 ** 3),
            cap(1024 ** 4),
            cap(1024 ** 5),
            cap(128 * (1024 ** 5)),
        ]);
        if (capacityUnits.join("|") !== "1.00 GiB|1.00 TiB|1.00 PiB|128.00 PiB") {
            throw new Error(`large-capacity formatting failed: ${capacityUnits.join("|")}`);
        }
        const overview = await page.locator("#storageOverview").innerText();
        if (!overview.includes("Thin pools") || !overview.includes("Thick Generations")) {
            throw new Error("overview did not render both allocation modes");
        }
        if (!overview.includes("must not be summed")) {
            throw new Error("same-VG capacity warning is missing");
        }
        await page.locator('.ni[data-page="storage"]').click();
        const storage = await page.locator("#storagePage").innerText();
        for (const expected of [
            "Thin pools",
            "Thick Generations",
            "PVE references",
            "warnings never trigger automatic cleanup or repair",
        ]) {
            if (!storage.includes(expected)) {
                throw new Error(`storage page is missing: ${expected}`);
            }
        }
        console.log("WEB_DUAL_MODE_LIVE_BROWSER=PASS");
    } finally {
        await browser.close();
    }
})().catch(error => {
    console.error(error.message);
    process.exit(1);
});
