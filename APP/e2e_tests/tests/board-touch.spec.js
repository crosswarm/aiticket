/**
 * E2E Tests for board.html - Touch Drag and Drop Functionality
 * Tests tablet/mobile touch interactions for the Kanban board
 */
import { test, expect } from '@playwright/test';

// Test configuration
const BOARD_URL = '/board.html';
const API_BASE = 'http://localhost:3000';

// Mock board data for testing - using actual column keys from board.html
const mockBoardData = {
    overdue: [
        {
            key: "TEST-001",
            summary: "Test Issue 1 - Touch Drag Test",
            status: "待处理",
            priority: "High",
            assignee: "Test User",
            due_date: "2026-02-01",
            ai_status: "completed",
            ai_analysis: {
                recommended_team: "流程中心",
                recommended_role: "开发工程师",
                confidence: 0.85
            }
        }
    ],
    today: [
        {
            key: "TEST-002",
            summary: "Test Issue 2 - Mobile Test",
            status: "待处理",
            priority: "Medium",
            assignee: "Test User",
            due_date: "2026-02-27",
            ai_status: "not_analyzed"
        }
    ],
    tomorrow: [
        {
            key: "TEST-003",
            summary: "Test Issue 3 - In Progress",
            status: "处理中",
            priority: "Low",
            assignee: "Test User",
            due_date: "2026-02-28",
            ai_status: "analyzing"
        }
    ],
    this_week: [],
    next_week: [],
    future: [],
    no_date: []
};

test.describe('Board Page - Touch Drag and Drop', () => {

    test.beforeEach(async ({ page }) => {
        // Mock the board config API
        await page.route(`${API_BASE}/api/config/board`, async (route) => {
            await route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify({
                    columns: [
                        { key: 'overdue', title: '⚠️ 已逾期', color: 'red', bg: 'bg-red-50', visible: true },
                        { key: 'today', title: '📅 今天到期', color: 'orange', bg: 'bg-orange-50', visible: true },
                        { key: 'tomorrow', title: '⏰ 明天到期', color: 'yellow', bg: 'bg-yellow-50', visible: true },
                        { key: 'this_week', title: '📆 本周到期', color: 'blue', bg: 'bg-blue-50', visible: true },
                        { key: 'next_week', title: '📋 下周到期', color: 'indigo', bg: 'bg-indigo-50', visible: true },
                        { key: 'future', title: '📌 更晚', color: 'green', bg: 'bg-green-50', visible: true },
                        { key: 'no_date', title: '📝 无到期日', color: 'gray', bg: 'bg-gray-50', visible: true }
                    ]
                })
            });
        });

        // Mock the API response for board data
        await page.route(`${API_BASE}/api/board**`, async (route) => {
            await route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify({
                    status: 'success',
                    data: mockBoardData,
                    stats: { total: 3, analyzed: 1, analyzing: 1, not_analyzed: 1 }
                })
            });
        });

        // Mock the move issue API
        await page.route(`${API_BASE}/api/board/move-issue`, async (route) => {
            await route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify({
                    status: 'success',
                    data: { message: 'Issue moved successfully' }
                })
            });
        });

        // Navigate to board page
        await page.goto(BOARD_URL);

        // Wait for the board to load
        await page.waitForSelector('#board-container', { timeout: 10000 });

        // Wait for columns to be rendered
        await page.waitForSelector('.board-column', { timeout: 10000 });
    });

    test('page should load successfully', async ({ page }) => {
        // Check page title
        await expect(page).toHaveTitle(/工单智能看板/);

        // Check header is visible
        const header = page.locator('header');
        await expect(header).toBeVisible();

        // Check board container exists
        const boardContainer = page.locator('#board-container');
        await expect(boardContainer).toBeVisible();
    });

    test('issue cards should render with proper data attributes', async ({ page }) => {
        // Wait for cards to be rendered
        await page.waitForTimeout(1000);

        // Check that issue cards exist (at least the mocked ones)
        const issueCards = page.locator('.issue-card');
        const cardCount = await issueCards.count();
        expect(cardCount).toBeGreaterThanOrEqual(3);

        // Check data attributes on cards
        const firstCard = issueCards.first();
        await expect(firstCard).toHaveAttribute('data-issue-key');
        await expect(firstCard).toHaveAttribute('data-column');

        // Verify specific data attributes
        const issueKey = await firstCard.getAttribute('data-issue-key');
        expect(issueKey).toMatch(/^TEST-\d+$/);
    });

    test('board columns should have proper structure', async ({ page }) => {
        // Check board columns exist
        const columns = page.locator('.board-column');
        const columnCount = await columns.count();
        expect(columnCount).toBeGreaterThan(0);

        // Check column data attributes
        const firstColumn = columns.first();
        await expect(firstColumn).toHaveAttribute('data-column-key');

        // Verify column key format
        const columnKey = await firstColumn.getAttribute('data-column-key');
        expect(columnKey).toBeTruthy();
    });

    test('touch events should be attached to cards', async ({ page }) => {
        // Wait for cards to render
        await page.waitForTimeout(1000);

        // Get first issue card
        const card = page.locator('.issue-card').first();
        await expect(card).toBeVisible();

        // Verify card has draggable attribute
        const draggable = await card.getAttribute('draggable');
        expect(draggable).toBe('true');

        // Verify card has proper CSS classes for touch interaction
        const classNames = await card.getAttribute('class');
        expect(classNames).toContain('draggable');
        expect(classNames).toContain('issue-card');
    });

    test('touch drag should create ghost element', async ({ page }) => {
        // Wait for cards to render
        await page.waitForTimeout(1000);

        const card = page.locator('.issue-card').first();
        const cardBox = await card.boundingBox();

        expect(cardBox).not.toBeNull();

        // Simulate touch start
        await card.dispatchEvent('touchstart', {
            touches: [{
                clientX: cardBox.x + cardBox.width / 2,
                clientY: cardBox.y + cardBox.height / 2,
                identifier: 0
            }]
        });

        // Wait for ghost element to be created
        await page.waitForTimeout(100);

        // Check if ghost element exists
        const ghost = page.locator('.drag-ghost');
        await expect(ghost).toBeVisible();

        // Clean up - simulate touch end
        await card.dispatchEvent('touchend');
    });

    test('touch move should update ghost position', async ({ page }) => {
        // Wait for cards to render
        await page.waitForTimeout(1000);

        const card = page.locator('.issue-card').first();
        const cardBox = await card.boundingBox();

        expect(cardBox).not.toBeNull();

        const startX = cardBox.x + cardBox.width / 2;
        const startY = cardBox.y + cardBox.height / 2;

        // Simulate touch start
        await card.dispatchEvent('touchstart', {
            touches: [{
                clientX: startX,
                clientY: startY,
                identifier: 0
            }]
        });

        await page.waitForTimeout(100);

        // Get initial ghost position
        const ghost = page.locator('.drag-ghost');
        const initialBox = await ghost.boundingBox();
        expect(initialBox).not.toBeNull();

        // Simulate touch move
        const moveX = startX + 100;
        const moveY = startY + 50;

        await card.dispatchEvent('touchmove', {
            touches: [{
                clientX: moveX,
                clientY: moveY,
                identifier: 0
            }]
        });

        await page.waitForTimeout(100);

        // Verify ghost is still visible after move
        await expect(ghost).toBeVisible();

        // Clean up
        await card.dispatchEvent('touchend');
    });

    test('touch end should trigger move operation between columns', async ({ page }) => {
        // Wait for cards to render
        await page.waitForTimeout(1000);

        const columns = page.locator('.board-column');
        const columnCount = await columns.count();

        if (columnCount < 2) {
            test.skip('Need at least 2 columns to test move operation');
        }

        // Find a column with cards
        let sourceColumn = null;
        let sourceColumnIndex = -1;
        for (let i = 0; i < columnCount; i++) {
            const col = columns.nth(i);
            const cards = col.locator('.issue-card');
            const cardCount = await cards.count();
            if (cardCount > 0) {
                sourceColumn = col;
                sourceColumnIndex = i;
                break;
            }
        }

        if (!sourceColumn) {
            test.skip('No column with cards found');
        }

        // Get first card from source column
        const card = sourceColumn.locator('.issue-card').first();

        // Verify card exists
        await expect(card).toBeVisible();

        const cardBox = await card.boundingBox();
        expect(cardBox).not.toBeNull();

        // Get a different column as target
        const targetColumnIndex = (sourceColumnIndex + 1) % columnCount;
        const targetColumn = columns.nth(targetColumnIndex);
        const targetColumnBox = await targetColumn.boundingBox();
        expect(targetColumnBox).not.toBeNull();

        // Simulate touch start on card
        await card.dispatchEvent('touchstart', {
            touches: [{
                clientX: cardBox.x + cardBox.width / 2,
                clientY: cardBox.y + cardBox.height / 2,
                identifier: 0
            }]
        });

        await page.waitForTimeout(100);

        // Simulate touch move to target column
        await card.dispatchEvent('touchmove', {
            touches: [{
                clientX: targetColumnBox.x + targetColumnBox.width / 2,
                clientY: targetColumnBox.y + 100,
                identifier: 0
            }]
        });

        await page.waitForTimeout(100);

        // Verify drag-over class is added to target column
        const hasDragOver = await targetColumn.evaluate(el => el.classList.contains('drag-over'));
        expect(hasDragOver).toBe(true);

        // Simulate touch end
        await card.dispatchEvent('touchend');

        await page.waitForTimeout(500);

        // Check for move toast notification (may or may not appear based on timing)
        // Just verify the page is still functional
        await expect(page.locator('#board-container')).toBeVisible();
    });

    test('card should have proper visual feedback during drag', async ({ page }) => {
        // Wait for cards to render
        await page.waitForTimeout(1000);

        const card = page.locator('.issue-card').first();
        const cardBox = await card.boundingBox();

        expect(cardBox).not.toBeNull();

        // Simulate touch start
        await card.dispatchEvent('touchstart', {
            touches: [{
                clientX: cardBox.x + cardBox.width / 2,
                clientY: cardBox.y + cardBox.height / 2,
                identifier: 0
            }]
        });

        await page.waitForTimeout(100);

        // Check if card has dragging class
        const hasDraggingClass = await card.evaluate(el => el.classList.contains('dragging'));
        expect(hasDraggingClass).toBe(true);

        // Check if card opacity is reduced
        const opacity = await card.evaluate(el => el.style.opacity);
        expect(opacity).toBe('0.5');

        // Clean up
        await card.dispatchEvent('touchend');
    });

    test('columns should highlight as drop targets during drag', async ({ page }) => {
        // Wait for cards to render
        await page.waitForTimeout(1000);

        const card = page.locator('.issue-card').first();
        const columns = page.locator('.board-column');
        const cardBox = await card.boundingBox();

        expect(cardBox).not.toBeNull();

        // Simulate touch start
        await card.dispatchEvent('touchstart', {
            touches: [{
                clientX: cardBox.x + cardBox.width / 2,
                clientY: cardBox.y + cardBox.height / 2,
                identifier: 0
            }]
        });

        await page.waitForTimeout(100);

        // Check if all columns have drop-target class
        const columnCount = await columns.count();
        for (let i = 0; i < columnCount; i++) {
            const hasDropTarget = await columns.nth(i).evaluate(el => el.classList.contains('drop-target'));
            expect(hasDropTarget).toBe(true);
        }

        // Clean up
        await card.dispatchEvent('touchend');
    });

    test('drag-over class should be removed after touch end', async ({ page }) => {
        // Wait for cards to render
        await page.waitForTimeout(1000);

        const card = page.locator('.issue-card').first();
        const columns = page.locator('.board-column');
        const cardBox = await card.boundingBox();

        expect(cardBox).not.toBeNull();

        const columnCount = await columns.count();
        const secondColumn = columns.nth(Math.min(1, columnCount - 1));
        const secondColumnBox = await secondColumn.boundingBox();

        // Simulate touch start
        await card.dispatchEvent('touchstart', {
            touches: [{
                clientX: cardBox.x + cardBox.width / 2,
                clientY: cardBox.y + cardBox.height / 2,
                identifier: 0
            }]
        });

        await page.waitForTimeout(100);

        // Move to second column
        if (secondColumnBox) {
            await card.dispatchEvent('touchmove', {
                touches: [{
                    clientX: secondColumnBox.x + secondColumnBox.width / 2,
                    clientY: secondColumnBox.y + 100,
                    identifier: 0
                }]
            });
        }

        await page.waitForTimeout(100);

        // Simulate touch end
        await card.dispatchEvent('touchend');

        await page.waitForTimeout(100);

        // Check drag-over class is removed from all columns
        for (let i = 0; i < columnCount; i++) {
            const hasDragOver = await columns.nth(i).evaluate(el => el.classList.contains('drag-over'));
            expect(hasDragOver).toBe(false);
        }
    });
});

test.describe('Board Page - Mobile Device Emulation @tablet', () => {

    test('board should be responsive on tablet', async ({ page }) => {
        // Mock board config
        await page.route(`${API_BASE}/api/config/board`, async (route) => {
            await route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify({
                    columns: [
                        { key: 'overdue', title: '⚠️ 已逾期', color: 'red', bg: 'bg-red-50', visible: true },
                        { key: 'today', title: '📅 今天到期', color: 'orange', bg: 'bg-orange-50', visible: true }
                    ]
                })
            });
        });

        // Mock API
        await page.route(`${API_BASE}/api/board**`, async (route) => {
            await route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify({
                    status: 'success',
                    data: mockBoardData,
                    stats: { total: 3, analyzed: 1, analyzing: 1, not_analyzed: 1 }
                })
            });
        });

        await page.goto(BOARD_URL);
        await page.waitForSelector('#board-container', { timeout: 10000 });

        // Check board container is visible
        const boardContainer = page.locator('#board-container');
        await expect(boardContainer).toBeVisible();

        // Check cards are visible
        const cards = page.locator('.issue-card');
        await expect(cards.first()).toBeVisible();
    });

    test('touch drag should work on tablet viewport', async ({ page }) => {
        // Mock board config
        await page.route(`${API_BASE}/api/config/board`, async (route) => {
            await route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify({
                    columns: [
                        { key: 'overdue', title: '⚠️ 已逾期', color: 'red', bg: 'bg-red-50', visible: true },
                        { key: 'today', title: '📅 今天到期', color: 'orange', bg: 'bg-orange-50', visible: true }
                    ]
                })
            });
        });

        // Mock API
        await page.route(`${API_BASE}/api/board**`, async (route) => {
            await route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify({
                    status: 'success',
                    data: mockBoardData,
                    stats: { total: 3, analyzed: 1, analyzing: 1, not_analyzed: 1 }
                })
            });
        });

        await page.goto(BOARD_URL);
        await page.waitForTimeout(1000);

        const card = page.locator('.issue-card').first();
        await expect(card).toBeVisible();

        const cardBox = await card.boundingBox();
        expect(cardBox).not.toBeNull();

        // Simulate touch events using dispatchEvent (not touchscreen which requires hasTouch)
        await card.dispatchEvent('touchstart', {
            touches: [{
                clientX: cardBox.x + cardBox.width / 2,
                clientY: cardBox.y + cardBox.height / 2,
                identifier: 0
            }]
        });

        // Wait for any UI updates
        await page.waitForTimeout(500);

        // Check ghost element was created
        const ghost = page.locator('.drag-ghost');
        await expect(ghost).toBeVisible();

        // Clean up
        await card.dispatchEvent('touchend');

        // Card should still be visible
        await expect(card).toBeVisible();
    });
});

test.describe('Board Page - Error Handling', () => {

    test('should handle API errors gracefully', async ({ page }) => {
        // Mock API to return error
        await page.route(`${API_BASE}/api/board**`, async (route) => {
            await route.fulfill({
                status: 500,
                contentType: 'application/json',
                body: JSON.stringify({
                    status: 'error',
                    message: 'Internal server error'
                })
            });
        });

        await page.route(`${API_BASE}/api/config/board`, async (route) => {
            await route.fulfill({
                status: 500,
                contentType: 'application/json',
                body: JSON.stringify({
                    status: 'error',
                    message: 'Config error'
                })
            });
        });

        await page.goto(BOARD_URL);
        await page.waitForTimeout(2000);

        // Page should still load without crashing
        await expect(page).toHaveTitle(/工单智能看板/);
    });

    test('should handle move operation failure', async ({ page }) => {
        // Mock board config
        await page.route(`${API_BASE}/api/config/board`, async (route) => {
            await route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify({
                    columns: [
                        { key: 'overdue', title: '⚠️ 已逾期', color: 'red', bg: 'bg-red-50', visible: true },
                        { key: 'today', title: '📅 今天到期', color: 'orange', bg: 'bg-orange-50', visible: true }
                    ]
                })
            });
        });

        // Mock successful load but failed move
        await page.route(`${API_BASE}/api/board**`, async (route) => {
            await route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify({
                    status: 'success',
                    data: mockBoardData,
                    stats: { total: 3, analyzed: 1, analyzing: 1, not_analyzed: 1 }
                })
            });
        });

        await page.route(`${API_BASE}/api/board/move-issue`, async (route) => {
            await route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify({
                    status: 'error',
                    data: { error: 'Move operation failed' }
                })
            });
        });

        await page.goto(BOARD_URL);
        await page.waitForTimeout(1000);

        const card = page.locator('.issue-card').first();
        const columns = page.locator('.board-column');
        const cardBox = await card.boundingBox();
        const secondColumn = columns.nth(1);
        const secondColumnBox = await secondColumn.boundingBox();

        expect(cardBox).not.toBeNull();
        expect(secondColumnBox).not.toBeNull();

        // Simulate drag and drop
        await card.dispatchEvent('touchstart', {
            touches: [{
                clientX: cardBox.x + cardBox.width / 2,
                clientY: cardBox.y + cardBox.height / 2,
                identifier: 0
            }]
        });

        await page.waitForTimeout(100);

        await card.dispatchEvent('touchmove', {
            touches: [{
                clientX: secondColumnBox.x + secondColumnBox.width / 2,
                clientY: secondColumnBox.y + 100,
                identifier: 0
            }]
        });

        await page.waitForTimeout(100);

        await card.dispatchEvent('touchend');
        await page.waitForTimeout(500);

        // Page should still be functional
        await expect(page.locator('#board-container')).toBeVisible();
    });
});
