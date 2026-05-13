import { test, expect } from '@playwright/test';

const weeklyReportFile = 'Weekly_Report_2026-03-09_2026-03-15.json';

function mockWeeklyReportApis(page) {
  return Promise.all([
    page.route('**/api/reports', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          {
            filename: weeklyReportFile,
            data_start_date: '2026-03-09',
            data_end_date: '2026-03-15',
            total_tickets: 12,
            period: '2026-03-09周报',
          },
        ]),
      });
    }),
    page.route(`**/api/reports/${weeklyReportFile}`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          meta: {
            filename: weeklyReportFile,
            data_start_date: '2026-03-09',
            data_end_date: '2026-03-15',
            generated_at: '2026-03-16 10:00:00',
            total_tickets: 12,
            count_process: 8,
            count_transferred: 2,
            ratio_transferred: 16.7,
          },
          charts: {
            type_counts: { 查询: 6, 建议: 4, 缺陷: 2 },
            role_counts: { 支持: 5, 研发: 4, 产品: 3 },
            daily_counts: {
              '2026-03-09': 2,
              '2026-03-10': 3,
              '2026-03-11': 1,
              '2026-03-12': 2,
              '2026-03-13': 4,
            },
          },
          content: '# 周总结\n\n本周流程中心重点问题集中在查询与审批效率。',
        }),
      });
    }),
  ]);
}

test.describe('报告 PDF 导出路由', () => {
  test('仅导出 PDF 时应走后端导出任务', async ({ page }) => {
    let exportPayload = null;
    let statusCalls = 0;

    await mockWeeklyReportApis(page);

    await page.route('**/api/export/report', async (route) => {
      exportPayload = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          status: 'success',
          task_id: 'export-pdf-1',
        }),
      });
    });

    await page.route('**/api/export/status/export-pdf-1', async (route) => {
      statusCalls += 1;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(
          statusCalls > 1
            ? {
                status: 'completed',
                progress: 100,
                message: '导出完成',
                output_files: ['weekly_demo.pdf'],
              }
            : {
                status: 'running',
                progress: 35,
                message: '正在生成 PDF',
                output_files: [],
              }
        ),
      });
    });

    await page.goto('/report.html');
    await expect(page.locator('#reportContent')).toContainText('周总结分析报告');

    await page.locator('#export-pdf').check();
    await page.locator('#export-md').uncheck();
    await page.locator('#export-docx').uncheck();
    await page.locator('#export-xlsx').uncheck();

    await page.locator('#exportBtn').click();

    await expect.poll(() => exportPayload).not.toBeNull();
    expect(exportPayload).toEqual({
      report_type: 'weekly',
      report_id: weeklyReportFile,
      formats: ['pdf'],
    });
    await expect.poll(() => statusCalls).toBeGreaterThan(1);
    await expect(page.locator('#exportBtnText')).toHaveText('导出');
  });

  test('打印视图应自动加载报告并隐藏控制区', async ({ page }) => {
    await mockWeeklyReportApis(page);

    await page.goto(`/report.html?print=1&type=weekly&id=${encodeURIComponent(weeklyReportFile)}`);

    await page.waitForFunction(() => window.__REPORT_PRINT_READY__ === true);
    await expect(page.locator('#reportContent')).toContainText('周总结分析报告');
    await expect(page.locator('#reportContent')).toContainText('本周流程中心重点问题');
    await expect(page.locator('.bg-slate-900').first()).toBeHidden();
    await expect(page.locator('#exportBtn')).toBeHidden();
  });
});
