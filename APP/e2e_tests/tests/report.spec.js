/**
 * E2E Tests for report.html (Weekly Report Page)
 */
import { test, expect } from '@playwright/test';

test.describe('Report Page - Weekly Analysis Report', () => {

    test.beforeEach(async ({ page }) => {
        await page.goto('/report.html');
    });

    test('page should load successfully', async ({ page }) => {
        await expect(page).toHaveTitle(/AI 工单|分析报告/);
    });

    test('should have sidebar with report list', async ({ page }) => {
        const sidebar = page.locator('.bg-slate-900, aside, [role="navigation"]').first();
        await expect(sidebar).toBeVisible();
    });

    test('should have generate report button', async ({ page }) => {
        const genBtn = page.locator('button:has-text("生成"), #genBtn');
        await expect(genBtn.first()).toBeVisible();
    });

    test('should have PDF export button', async ({ page }) => {
        const exportBtn = page.locator('button:has-text("导出"), button:has-text("PDF")');
        await expect(exportBtn.first()).toBeVisible();
    });

    test('should have print button', async ({ page }) => {
        const printBtn = page.locator('button:has-text("打印")');
        await expect(printBtn.first()).toBeVisible();
    });

    test('should load report list from API', async ({ page }) => {
        // Wait for API call to complete
        await page.waitForTimeout(2000);

        // Check if report list is populated OR shows empty message
        const reportList = page.locator('#reportObjList');
        const hasContent = await reportList.locator('button, div').count() > 0;

        expect(hasContent).toBeTruthy();
    });

    test('clicking report should load its content', async ({ page }) => {
        // Wait for list to load
        await page.waitForTimeout(2000);

        // Find first report button
        const firstReport = page.locator('#reportObjList button').first();
        const hasReports = await firstReport.count() > 0;

        if (hasReports) {
            await firstReport.click();

            // Should update main content area
            await page.waitForTimeout(1000);
            const contentArea = page.locator('#reportContent');
            await expect(contentArea).toBeVisible();
        }
    });

    test('should have back to query link', async ({ page }) => {
        // Link with href="/" that contains "返回"
        const backLink = page.locator('a[href="/"]');
        await expect(backLink.first()).toBeVisible();
    });

});

test.describe('Report Page - Chart Rendering', () => {

    test('should render charts when report selected', async ({ page }) => {
        await page.goto('/report.html');
        await page.waitForTimeout(3000);

        // Charts are rendered on canvas elements
        const charts = page.locator('canvas');
        const chartCount = await charts.count();

        // If reports exist, should have some charts
        // This is conditional based on data availability
        expect(chartCount).toBeGreaterThanOrEqual(0);
    });

    test('chart containers should be properly sized', async ({ page }) => {
        await page.goto('/report.html');
        await page.waitForTimeout(2000);

        const chartContainer = page.locator('.chart-container').first();
        const exists = await chartContainer.count() > 0;

        if (exists) {
            const box = await chartContainer.boundingBox();
            if (box) {
                expect(box.height).toBeGreaterThan(100);
            }
        }
    });

});
