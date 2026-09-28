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
    const expectedProfile = required("SLT_EXPECT_PROFILE");
    if (!["dual", "thick-only"].includes(expectedProfile)) {
        throw new Error("SLT_EXPECT_PROFILE must be dual or thick-only");
    }
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
        const health = await page.evaluate(async () => {
            const response = await fetch("/api/health", { cache: "no-store" });
            if (!response.ok) throw new Error(`health API returned HTTP ${response.status}`);
            return response.json();
        });
        if (health.platform?.package_flavor !== expectedProfile) {
            throw new Error(`package profile mismatch: expected ${expectedProfile}, observed ${health.platform?.package_flavor}`);
        }
        if (expectedProfile === "thick-only" && health.storages.some(
            storage => (storage.allocation_mode || "thin") !== "thick-generations"
        )) {
            throw new Error("Thick-only health data contains a non-Thick storage");
        }
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
        if (!overview.includes("Thick Generations")) {
            throw new Error("overview did not render Thick Generations");
        }
        if (expectedProfile === "dual" && !overview.includes("Thin pools")) {
            throw new Error("Dual overview did not render Thin pools");
        }
        if (expectedProfile === "thick-only" && overview.includes("Thin pools")) {
            throw new Error("Thick-only overview unexpectedly rendered Thin pools");
        }
        if (!overview.includes("must not be summed")) {
            throw new Error("same-VG capacity warning is missing");
        }
        await page.locator('.ni[data-page="storage"]').click();
        const storage = await page.locator("#storagePage").innerText();
        const expectedStorageText = [
            "Thick Generations",
            "PVE references",
            "warnings never trigger automatic cleanup or repair",
        ];
        if (expectedProfile === "dual") expectedStorageText.push("Thin pools");
        for (const expected of expectedStorageText) {
            if (!storage.includes(expected)) {
                throw new Error(`storage page is missing: ${expected}`);
            }
        }
        if (expectedProfile === "thick-only" && storage.includes("Thin pools")) {
            throw new Error("Thick-only storage page unexpectedly rendered Thin pools");
        }
        console.log(`WEB_PACKAGE_LIVE_BROWSER=PASS PROFILE=${expectedProfile}`);
    } finally {
        await browser.close();
    }
})().catch(error => {
    console.error(error.message);
    process.exit(1);
});
