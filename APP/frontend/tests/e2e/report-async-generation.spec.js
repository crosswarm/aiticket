import { test, expect } from '@playwright/test';

test.describe('报告异步生成', () => {
  test('周报生成应走异步任务并显示进度', async ({ page }) => {
    let statusCalls = 0;

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
              filename: '工作流-周数据-2026-03-09-2026-03-15T19_58_40+0800.csv',
              has_report: false,
              report_filename: null,
              data_period: '2026-03-09 至 2026-03-15',
              modified_time: '2026-03-16 09:00:00',
            },
          ],
        }),
      });
    });

    await page.route('**/api/report/start', async (route) => {
      const payload = route.request().postDataJSON();
      expect(payload.task_type).toBe('weekly');
      expect(payload.csv_filename).toBe('工作流-周数据-2026-03-09-2026-03-15T19_58_40+0800.csv');
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          status: 'started',
          task_id: 'weekly-task-1',
        }),
      });
    });

    await page.route('**/api/report/status/weekly-task-1', async (route) => {
      statusCalls += 1;
      const body = statusCalls > 1
        ? {
            task_id: 'weekly-task-1',
            task_type: 'weekly',
            status: 'completed',
            progress: 100,
            message: '周报生成完成',
            result: { meta: { filename: 'Weekly_Report_2026-03-09_2026-03-15.json' } },
            error: null,
          }
        : {
            task_id: 'weekly-task-1',
            task_type: 'weekly',
            status: 'running',
            progress: 55,
            message: '分析工单趋势中',
            result: null,
            error: null,
          };
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(body),
      });
    });

    await page.goto('/report.html');
    await page.click('#genBtn');
    await expect(page.locator('#csvSelectModal')).toBeVisible();
    await page.click('button:has-text("生成报告")');

    await expect(page.locator('#report-task-panel')).toBeVisible();
    await expect(page.locator('#report-task-panel')).toContainText('周报生成任务');
    await expect(page.locator('#report-task-panel')).toContainText('55%');
    await expect(page.locator('#report-task-panel')).toContainText('分析工单趋势中');
    await expect.poll(() => statusCalls).toBeGreaterThan(1);
    await expect(page.locator('#report-task-panel')).toContainText('已完成');
  });
});
