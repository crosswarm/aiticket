import { test, expect } from '@playwright/test';

test.describe('周报 CSV 数据源选择', () => {
  test('生成周报应兼容对象结构的 csv_sources 返回值', async ({ page }) => {
    page.on('dialog', dialog => {
      throw new Error(`unexpected dialog: ${dialog.message()}`);
    });

    await page.route('**/api/reports', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([]),
      });
    });

    await page.route('**/api/weekly_report/csv_sources', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          sources: [
            {
              filename: '工作流-周数据-2026-03-02-2026-03-08.csv',
              has_report: false,
              report_filename: null,
              data_period: '2026-03-02 至 2026-03-08',
              modified_time: '2026-03-09 10:00:00',
            },
          ],
        }),
      });
    });

    await page.goto('/report.html');
    await page.click('#genBtn');

    await expect(page.locator('#csvSelectModal')).toBeVisible();
    await expect(page.locator('#csvSelectModal')).toContainText('选择数据源生成周报');
    await expect(page.locator('#csvSelectModal')).toContainText('工作流-周数据-2026-03-02-2026-03-08.csv');
    await expect(page.locator('input[name="csv_source"]')).toHaveCount(1);
  });
});
