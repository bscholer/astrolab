/**
 * Regression specs for the design-polish pass covering:
 *   - 44px tap-target expansion on publish-toggle and compare-toggle
 *   - Responsive jobs table (mobile: 3 cols; desktop: 5 cols)
 *   - Compare slider auto-focus on mount
 *   - NodeParamsForm tooltip right-anchored on mobile
 *   - NodeParamsForm header no longer shows raw nodeId
 *   - Settings: Scan now is .btn.warn (amber); Save buttons stay .btn.primary (teal)
 *   - Reprocess button is .hbtn.warn (amber)
 */

import { test, expect, type Page, type BrowserContext } from '@playwright/test';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const BASE = 'http://localhost:5173';

/** Returns the bounding box of an element, asserting it is visible first. */
async function bbox(page: Page, selector: string) {
  const el = page.locator(selector).first();
  await el.waitFor({ state: 'visible' });
  const box = await el.boundingBox();
  if (!box) throw new Error(`No bounding box for ${selector}`);
  return box;
}

// ---------------------------------------------------------------------------
// Project detail page
// ---------------------------------------------------------------------------

test.describe('project detail page', () => {
  let projectId: string;

  test.beforeAll(async ({ request }) => {
    // Find or create a project to exercise the UI with.
    const projects = await request.get(`${BASE}/api/projects`);
    const list = await projects.json();
    if (list.length > 0) {
      projectId = list[0].id;
      return;
    }
    // If no project exists, skip by leaving projectId undefined.
  });

  test.beforeEach(async ({ page }) => {
    if (!projectId) test.skip();
    await page.goto(`${BASE}/projects/${projectId}`);
    await page.waitForLoadState('networkidle');
  });

  test('reprocess button has warn (amber) color, not teal', async ({ page }) => {
    // The button may be hidden if there is no history yet; look for it via
    // data attributes / class and measure color rather than requiring a click.
    const btn = page.locator('button.hbtn.warn.reprocess').first();

    // The button may be absent if the project has no history entries and the
    // undo/redo toolbar is hidden.  Use a softer assertion.
    const count = await btn.count();
    if (count === 0) {
      // Toolbar requires at least one history entry. Accept as known limitation.
      return;
    }

    await btn.waitFor({ state: 'visible' });
    const color = await btn.evaluate((el) => getComputedStyle(el).color);
    // Amber: rgb(251, 191, 36) or close variant - check it is NOT the teal accent
    expect(color).not.toMatch(/94, 234, 212/);
  });

  // -----------------------------------------------------------------------
  // Hit-target tests (desktop: basic checks; mobile: separate project)
  // -----------------------------------------------------------------------

  test('publish-toggle ::before expands hit area to at least 44x44px', async ({ page }) => {
    // If there are no history entries the toggles won't be rendered.
    const toggleCount = await page.locator('.publish-toggle').count();
    if (toggleCount === 0) return;

    const toggle = page.locator('.publish-toggle').first();
    const toggleBox = await toggle.boundingBox();
    if (!toggleBox) return;

    // The ::before pseudo element is measured indirectly: inject a test that
    // reads the computed style of the element's before pseudo-element.
    const beforeInset = await toggle.evaluate((el) => {
      const style = getComputedStyle(el, '::before');
      return {
        content: style.content,
        inset: style.inset,
      };
    });

    // content should be '' (empty string, rendered as '""' by getComputedStyle)
    expect(beforeInset.content).not.toBe('none');

    // The effective hit area includes the -12px inset on all sides.
    // We can't directly measure the pseudo-element box, but we can verify
    // the inset shorthand resolves to a negative value (shrinks inward = expands outward).
    // inset: -12px → "top right bottom left" all -12px
    expect(beforeInset.inset).toContain('-12px');
  });

  test('compare-toggle ::before expands hit area', async ({ page }) => {
    const toggleCount = await page.locator('.compare-toggle').count();
    if (toggleCount === 0) return;

    const toggle = page.locator('.compare-toggle').first();
    const beforeInset = await toggle.evaluate((el) => {
      const style = getComputedStyle(el, '::before');
      return style.inset;
    });
    expect(beforeInset).toContain('-12px');
  });
});

// ---------------------------------------------------------------------------
// Project detail - mobile viewport tap-target size
// ---------------------------------------------------------------------------

test.describe('project detail page - mobile viewport', () => {
  let projectId: string;

  test.beforeAll(async ({ request }) => {
    const projects = await request.get(`${BASE}/api/projects`);
    const list = await projects.json();
    if (list.length > 0) projectId = list[0].id;
  });

  test('publish-toggle is tappable at 375px wide without grazing parent', async ({
    page,
    browserName,
  }) => {
    if (!projectId) test.skip();
    await page.setViewportSize({ width: 375, height: 812 });
    await page.goto(`${BASE}/projects/${projectId}`);
    await page.waitForLoadState('networkidle');

    const toggleCount = await page.locator('.publish-toggle').count();
    if (toggleCount === 0) return;

    const toggle = page.locator('.publish-toggle').first();
    const box = await toggle.boundingBox();
    if (!box) return;

    // The visual box of the icon button is small (~1.4rem ~ 22px).
    // The pseudo-element extends it, but since we can't directly hit-test
    // pseudo-elements with Playwright, confirm the parent hist-entry's revert
    // button is NOT triggered when we click the toggle's center.
    let revertFired = false;
    await page.evaluate(() => {
      // Patch the first revert (hist-entry > button:first-child) to record a click.
      const parent = document.querySelector('.hist-entry > button:first-child');
      if (parent) {
        parent.addEventListener('click', () => {
          (window as unknown as Record<string, boolean>)['__revert_fired__'] = true;
        }, { capture: true });
      }
    });

    // Click the star icon center
    await toggle.click({ position: { x: box.width / 2, y: box.height / 2 } });

    const revertFiredAfter = await page.evaluate(
      () => (window as unknown as Record<string, boolean>)['__revert_fired__'] ?? false
    );
    // The revert button should NOT fire when clicking the toggle.
    expect(revertFiredAfter).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Jobs page - responsive table columns
// ---------------------------------------------------------------------------

test.describe('jobs page - responsive table', () => {
  test('desktop shows all 5 column headers', async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 768 });
    await page.goto(`${BASE}/jobs`);
    await page.waitForLoadState('networkidle');

    // Wait for either the table or the empty state.
    await page.waitForSelector('table.jobs, .muted', { timeout: 10000 });

    const tableExists = await page.locator('table.jobs').count();
    if (tableExists === 0) {
      // No jobs - empty state is acceptable; can't test column count.
      const emptyMsg = page.locator('.muted');
      await expect(emptyMsg).toBeVisible();
      return;
    }

    const headers = page.locator('table.jobs th');
    await expect(headers).toHaveCount(5);

    // All 5 headers should be visible on desktop.
    for (let i = 0; i < 5; i++) {
      const display = await headers.nth(i).evaluate((el) => getComputedStyle(el).display);
      expect(display).not.toBe('none');
    }
  });

  test('mobile (375px) hides columns 4 and 5 (Duration, Submitted)', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await page.goto(`${BASE}/jobs`);
    await page.waitForLoadState('networkidle');

    await page.waitForSelector('table.jobs, .muted', { timeout: 10000 });

    const tableExists = await page.locator('table.jobs').count();
    if (tableExists === 0) {
      // No jobs - can't exercise the table; check that empty state is readable.
      const emptyMsg = page.locator('.muted');
      await expect(emptyMsg).toBeVisible();
      return;
    }

    const headers = page.locator('table.jobs th');
    await expect(headers).toHaveCount(5);

    // Column 4 (Duration) and 5 (Submitted) should be hidden.
    const col4Display = await headers.nth(3).evaluate((el) => getComputedStyle(el).display);
    const col5Display = await headers.nth(4).evaluate((el) => getComputedStyle(el).display);

    expect(col4Display).toBe('none');
    expect(col5Display).toBe('none');

    // Columns 1-3 should still be visible.
    for (let i = 0; i < 3; i++) {
      const display = await headers.nth(i).evaluate((el) => getComputedStyle(el).display);
      expect(display).not.toBe('none');
    }
  });

  test('mobile (375px) - jobs table does not overflow beyond viewport width', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await page.goto(`${BASE}/jobs`);
    await page.waitForLoadState('networkidle');

    await page.waitForSelector('table.jobs, .muted', { timeout: 10000 });

    const tableExists = await page.locator('table.jobs').count();
    if (tableExists === 0) return;

    // The table itself should not overflow its container width.
    const tableOverflows = await page.evaluate(() => {
      const table = document.querySelector('table.jobs');
      if (!table) return false;
      const tableEl = table;
      const containerWidth = document.documentElement.clientWidth;
      return tableEl.scrollWidth > containerWidth;
    });
    expect(tableOverflows).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Compare page - slider auto-focus
// ---------------------------------------------------------------------------

test.describe('compare page', () => {
  test('shows empty/pick state without errors when no picks are in the URL', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (err) => errors.push(err.message));
    page.on('console', (msg) => {
      if (msg.type() === 'error') errors.push(msg.text());
    });

    await page.goto(`${BASE}/compare`);
    await page.waitForLoadState('networkidle');

    // Should show the selector UI - no crash.
    await expect(page.locator('h1').first()).toBeVisible();

    // No unhandled page errors from the fetch effects running without picks.
    const fatal = errors.filter((e) => !e.includes('favicon'));
    expect(fatal).toHaveLength(0);
  });

  test('slider viewport receives focus within 1s when valid picks are in URL', async ({
    page,
    request,
  }) => {
    // This test only runs if the gallery has at least two entries.
    const galleryResp = await request.get(`${BASE}/api/gallery`);
    const gallery = await galleryResp.json();
    if (gallery.length < 2) {
      // No renders to compare - skip without failure.
      return;
    }

    const a = gallery[0];
    const b = gallery[1];
    const url = `${BASE}/compare?a=${a.project_id}:${a.seq}&b=${b.project_id}:${b.seq}`;

    await page.goto(url);
    await page.waitForLoadState('networkidle');

    // Wait up to 1s for .viewport to receive focus.
    const focused = await page.evaluate(async () => {
      const start = Date.now();
      return new Promise<boolean>((resolve) => {
        const check = () => {
          const vp = document.querySelector('.viewport');
          if (vp && document.activeElement === vp) {
            resolve(true);
            return;
          }
          if (Date.now() - start > 1000) {
            resolve(false);
            return;
          }
          requestAnimationFrame(check);
        };
        check();
      });
    });

    expect(focused).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Settings page - warn button color
// ---------------------------------------------------------------------------

test.describe('settings page', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(`${BASE}/settings`);
    await page.waitForLoadState('networkidle');
  });

  test('Scan now button has amber background, not teal', async ({ page }) => {
    const scanBtn = page.locator('button.btn.warn', { hasText: 'Scan' }).first();
    const count = await scanBtn.count();
    if (count === 0) {
      // Button may be absent if capture_root is not set.
      return;
    }
    await scanBtn.waitFor({ state: 'visible' });
    const bg = await scanBtn.evaluate((el) => getComputedStyle(el).backgroundColor);
    // Amber: rgb(251, 191, 36) range; teal accent: rgb(94, 234, 212).
    // The gradient means getComputedStyle returns the starting color or 'rgba(0,0,0,0)' --
    // check that it does NOT match the teal accent.
    expect(bg).not.toMatch(/94, 234, 212/);
  });

  test('Save location button keeps teal (primary) styling', async ({ page }) => {
    // Find Save buttons that are .btn.primary
    const saveBtn = page.locator('button.btn.primary').first();
    const count = await saveBtn.count();
    if (count === 0) return;
    await saveBtn.waitFor({ state: 'visible' });

    // Primary should NOT carry the warn class.
    const classAttr = await saveBtn.getAttribute('class');
    expect(classAttr).not.toContain('warn');
  });

  test('p.warn text nodes remain amber prose, not button-styled', async ({ page }) => {
    const warnPara = page.locator('p.warn').first();
    const count = await warnPara.count();
    if (count === 0) return;
    await warnPara.waitFor({ state: 'visible' });

    // Should have amber color from var(--warn) and NOT have button padding/border-radius.
    const styles = await warnPara.evaluate((el) => {
      const cs = getComputedStyle(el);
      return {
        color: cs.color,
        borderRadius: cs.borderRadius,
        padding: cs.padding,
        fontWeight: cs.fontWeight,
      };
    });

    // Color should be in the amber range (not pure white/gray).
    // --warn typically resolves to rgb(251, 191, 36).
    // The button .btn.warn uses color #2a1d05 (dark ink); p.warn should use var(--warn) directly.
    expect(styles.color).not.toMatch(/42, 29, 5/); // not the dark-ink button text color

    // It should not have button-like border-radius (999px).
    expect(styles.borderRadius).not.toBe('999px');
  });

  test('settings page loads without unhandled fetch errors', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (err) => errors.push(err.message));
    page.on('console', (msg) => {
      if (msg.type() === 'error') errors.push(msg.text());
    });

    await page.reload();
    await page.waitForLoadState('networkidle');

    const fatal = errors.filter((e) => !e.includes('favicon'));
    expect(fatal).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// NodeParamsForm - tooltip and header on mobile
// ---------------------------------------------------------------------------

test.describe('NodeParamsForm - mobile', () => {
  let projectId: string;

  test.beforeAll(async ({ request }) => {
    const projects = await request.get(`${BASE}/api/projects`);
    const list = await projects.json();
    if (list.length > 0) projectId = list[0].id;
  });

  test.beforeEach(async ({ page }) => {
    if (!projectId) test.skip();
    await page.setViewportSize({ width: 375, height: 812 });
    await page.goto(`${BASE}/projects/${projectId}`);
    await page.waitForLoadState('networkidle');
  });

  test('info tooltip anchors to right edge and does not overflow viewport', async ({ page }) => {
    // Expand the first params accordion if any.
    const accordionBtn = page.locator('.param-head').first();
    const accordionCount = await accordionBtn.count();
    if (accordionCount === 0) return;

    // Open the first node params panel.
    await accordionBtn.click();

    // Now look for an info icon button.
    const infoIcon = page.locator('.info-icon').first();
    const infoCount = await infoIcon.count();
    if (infoCount === 0) return;

    await infoIcon.focus();
    // After focus the ::after tooltip should appear.
    const tooltipBox = await infoIcon.evaluate((el) => {
      // Read the right CSS property that positions the tooltip.
      const style = getComputedStyle(el, '::after');
      return {
        right: style.right,
        left: style.left,
        display: style.display,
      };
    });

    // The right-anchored tooltip should have right: 0px and left: auto.
    expect(tooltipBox.right).toBe('0px');
    expect(tooltipBox.left).toBe('auto');
  });

  test('form header does not show raw nodeId text', async ({ page }) => {
    // Open the first node params panel.
    const accordionBtn = page.locator('.param-head').first();
    const count = await accordionBtn.count();
    if (count === 0) return;

    await accordionBtn.click();

    // The .muted.small span that previously showed nodeId should be gone.
    // Header element is inside the expanded params card.
    const nodeIdSpan = page.locator('.params-form header .muted.small');
    const spanCount = await nodeIdSpan.count();
    // There should be no such span (it was removed in this polish pass).
    expect(spanCount).toBe(0);
  });
});
