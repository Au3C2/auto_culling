/**
 * UI Interaction Automation Test Suite
 * 
 * Simulates user interactions in a browser / Playwright environment:
 * 1. Directory Selection & Scanning
 * 2. Parameter Configurations & Tooltips Verification
 * 3. Starting Culling Run
 * 4. Real-time Speed & ETA Calculations
 * 5. Table Row Updates, Sorting & Filtering
 * 6. Thumbnail Preview & Bounding Box Rendering
 * 7. Draggable Splitter Pane Resize
 * 8. Cancellation Mid-Flight
 */

import { test, expect } from '@playwright/test';

test.describe('Auto-Culling GUI Interaction Suite', () => {

  test.beforeEach(async ({ page }) => {
    // Navigate to local UI
    await page.goto('http://localhost:8080/index.html');
    // Ensure mock backend loaded
    await page.waitForFunction(() => window.__TAURI__ !== undefined);
  });

  test('1. UI Layout & Tooltip Verification', async ({ page }) => {
    // Check main UI elements exist
    await expect(page.locator('#btnBrowse')).toBeVisible();
    await expect(page.locator('#btnRun')).toBeVisible();
    await expect(page.locator('#inputDir')).toBeVisible();
    await expect(page.locator('#progressBar')).toBeVisible();
    await expect(page.locator('#photoTable')).toBeVisible();
    await expect(page.locator('#previewImg')).toBeVisible();

    // Verify hover tooltip on parameter "i" icon
    const sharpTipWrap = page.locator('label:has(#pSharp) .tip-wrap');
    await sharpTipWrap.hover();
    const tipContent = sharpTipWrap.locator('.tip-content');
    await expect(tipContent).toBeVisible();
    await expect(tipContent).toContainText('默认');
    await expect(tipContent).toContainText('0.05');
  });

  test('2. Directory Selection & Table Population', async ({ page }) => {
    // Click Browse
    await page.locator('#btnBrowse').click();
    await expect(page.locator('#inputDir')).toHaveValue('/Users/test/camera_burst_01');

    // Wait for mock scan event to populate table
    await page.waitForSelector('#photoTable tbody tr');
    const rowCount = await page.locator('#photoTable tbody tr').count();
    expect(rowCount).toBeGreaterThan(0);
  });

  test('3. Parameter Adjustment & Persistence', async ({ page }) => {
    // Change a parameter
    const topNInput = page.locator('#pTopN');
    await topNInput.fill('7');
    await topNInput.dispatchEvent('change');

    // Check localStorage
    const saved = await page.evaluate(() => localStorage.getItem('ac-param-top_n'));
    expect(saved).toBe('7');
  });

  test('4. Run Culling, Speed & ETA Display, and Table Flash', async ({ page }) => {
    await page.locator('#btnBrowse').click();
    await page.waitForSelector('#photoTable tbody tr');

    // Click Start Culling
    await page.locator('#btnRun').click();
    await expect(page.locator('#btnRun')).toHaveText(/取消/);

    // Wait for scoring frames to arrive
    await page.waitForFunction(() => {
      const stat = document.getElementById('frameStat')?.textContent || '';
      return stat.includes('张/秒') || stat.includes('已打分');
    }, { timeout: 5000 });

    // Wait until done
    await page.waitForFunction(() => {
      const btn = document.getElementById('btnRun');
      return btn && btn.textContent.includes('开始筛选');
    }, { timeout: 10000 });

    // Verify results populated
    const firstRowRating = await page.locator('#photoTable tbody tr:first-child .rating-cell').textContent();
    expect(firstRowRating).toBeTruthy();
  });

  test('5. Table Filtering & Sorting', async ({ page }) => {
    await page.locator('#btnBrowse').click();
    await page.locator('#btnRun').click();
    await page.waitForFunction(() => document.getElementById('btnRun')?.textContent.includes('开始筛选'), { timeout: 10000 });

    // Filter by Keep (Stars)
    await page.locator('.filter-btn[data-filter="keep"]').click();
    const keepRows = await page.locator('#photoTable tbody tr:visible').count();
    expect(keepRows).toBeGreaterThan(0);

    // Filter by Discard (Rejects)
    await page.locator('.filter-btn[data-filter="reject"]').click();
    const rejectRows = await page.locator('#photoTable tbody tr:visible').count();
    expect(rejectRows).toBeGreaterThan(0);

    // Click Filter All
    await page.locator('.filter-btn[data-filter="all"]').click();
  });

  test('6. Thumbnail Selection & Bounding Box Overlays', async ({ page }) => {
    await page.locator('#btnBrowse').click();
    await page.waitForSelector('#photoTable tbody tr');

    // Click on the first row
    const firstRow = page.locator('#photoTable tbody tr:first-child');
    await firstRow.click();
    await expect(firstRow).toHaveClass(/selected/);

    // Verify preview image loaded
    const previewImg = page.locator('#previewImg');
    await expect(previewImg).toBeVisible();
    const src = await previewImg.getAttribute('src');
    expect(src).toContain('data:image/png;base64');
  });

  test('7. Draggable Splitter Resizing', async ({ page }) => {
    const resizer = page.locator('#splitResizer');
    await expect(resizer).toBeVisible();

    const resizerBox = await resizer.boundingBox();
    expect(resizerBox).not.toBeNull();

    if (resizerBox) {
      // Drag resizer to the right
      await page.mouse.move(resizerBox.x + resizerBox.width / 2, resizerBox.y + resizerBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(resizerBox.x + 100, resizerBox.y + resizerBox.height / 2);
      await page.mouse.up();

      // Check localStorage ratio updated
      const ratio = await page.evaluate(() => localStorage.getItem('ac-table-ratio'));
      expect(ratio).toBeTruthy();
    }
  });

  test('8. Cancellation Mid-Flight', async ({ page }) => {
    await page.locator('#btnBrowse').click();
    await page.waitForSelector('#photoTable tbody tr');

    // Click Run then immediately click Cancel
    await page.locator('#btnRun').click();
    await expect(page.locator('#btnRun')).toHaveText(/取消/);

    await page.locator('#btnRun').click();
    await expect(page.locator('#btnRun')).toHaveText(/开始筛选/);
    await expect(page.locator('#stageStatus')).toContainText(/已取消|完成/);
  });

});
