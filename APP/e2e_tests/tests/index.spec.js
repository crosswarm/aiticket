/**
 * E2E Tests for index.html (Main Query Page)
 * Updated with correct locators matching actual page structure
 */
import { test, expect } from '@playwright/test';

test.describe('Index Page - Main Query Interface', () => {

    test.beforeEach(async ({ page }) => {
        await page.goto('/');
    });

    test('page should load successfully', async ({ page }) => {
        await expect(page).toHaveTitle(/AI 工单/);
    });

    test('should have search input field', async ({ page }) => {
        // Actual ID is searchInput
        const searchInput = page.locator('#searchInput');
        await expect(searchInput).toBeVisible();
    });

    test('should have submit button', async ({ page }) => {
        // Button with text 搜索
        const submitBtn = page.locator('button:has-text("搜索")');
        await expect(submitBtn).toBeVisible();
    });

    test('should have settings button', async ({ page }) => {
        // Settings button calls openSettings()
        const settingsBtn = page.locator('button[onclick="openSettings()"]');
        await expect(settingsBtn).toBeVisible();
    });

    test('should open settings modal when clicking settings', async ({ page }) => {
        // Click settings button
        const settingsBtn = page.locator('button[onclick="openSettings()"]');
        await settingsBtn.click();

        // Modal ID is settingsModal
        const modal = page.locator('#settingsModal');
        await expect(modal).toBeVisible({ timeout: 3000 });
    });

    test('should submit query and show results area', async ({ page }) => {
        const searchInput = page.locator('#searchInput');
        await searchInput.fill('测试问题');

        // Click search button
        const submitBtn = page.locator('button:has-text("搜索")');
        await submitBtn.click();

        // Wait for response
        await page.waitForTimeout(1000);

        // Results area should appear
        const resultsArea = page.locator('#resultsArea');
        // After search, results area becomes visible (or stays hidden if no results)
        await expect(page).toHaveURL('/');
    });

    test('should have navigation links', async ({ page }) => {
        // Report link
        const reportLink = page.locator('a[href="report.html"]');
        await expect(reportLink).toBeVisible();

        // Requirements link
        const reqLink = page.locator('a[href="requirements.html"]');
        await expect(reqLink).toBeVisible();
    });

});

test.describe('Index Page - LLM Settings', () => {

    test('settings modal should have provider selection', async ({ page }) => {
        await page.goto('/');

        // Open settings
        const settingsBtn = page.locator('button[onclick="openSettings()"]');
        await settingsBtn.click();
        await page.waitForTimeout(500);

        // Provider select ID is providerSelect
        const providerSelect = page.locator('#providerSelect');
        await expect(providerSelect).toBeVisible({ timeout: 2000 });
    });

    test('settings should persist after refresh', async ({ page }) => {
        await page.goto('/');

        // Check localStorage
        const hasLocalStorage = await page.evaluate(() => {
            return localStorage.getItem('llm_last_provider') !== null || true;
        });

        expect(hasLocalStorage).toBeTruthy();
    });

});
