import { defineConfig } from '@playwright/test';

export default defineConfig({
    testDir: './tests',
    timeout: 30000,
    expect: {
        timeout: 5000
    },
    use: {
        baseURL: 'http://localhost:3000',
        headless: true,
        screenshot: 'only-on-failure',
        trace: 'retain-on-failure',
    },
    reporter: [
        ['html', { outputFolder: 'playwright-report' }],
        ['list']
    ],
    projects: [
        {
            name: 'chromium',
            use: { browserName: 'chromium' },
        }
    ],
});
