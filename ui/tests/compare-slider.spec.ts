/**
 * Regression: CompareSlider used to clip only the top image, so when the two
 * versions were cropped or shaped differently the other one showed through
 * the margins (and the left side showed B under an A label). Each side must
 * show only its own image.
 *
 * Self-contained: the gallery, project and preview endpoints are mocked, so
 * it needs only the UI dev server, not the API.
 */
import { test, expect, type Page } from '@playwright/test';

async function solidPng(page: Page, w: number, h: number, color: string): Promise<Buffer> {
  const b64 = await page.evaluate(
    async ([w, h, color]) => {
      const c = new OffscreenCanvas(w as number, h as number);
      const g = c.getContext('2d')!;
      g.fillStyle = color as string;
      g.fillRect(0, 0, w as number, h as number);
      const blob = await c.convertToBlob({ type: 'image/png' });
      const buf = new Uint8Array(await blob.arrayBuffer());
      let s = '';
      for (const b of buf) s += String.fromCharCode(b);
      return btoa(s);
    },
    [w, h, color]
  );
  return Buffer.from(b64, 'base64');
}

test('each side of the compare slider shows only its own image', async ({ page }) => {
  await page.goto('/');
  const wideRed = await solidPng(page, 800, 200, '#ff0000'); // A: wide crop
  const tallBlue = await solidPng(page, 200, 800, '#0000ff'); // B: tall crop

  const entry = (seq: number, hash: string) => ({
    project_id: 'p1', project_name: 'Test', target_common_name: null, template_id: 't',
    seq, label: null, created_at: '2026-01-01T00:00:00Z', preview_hash: hash,
    preview_port: 'image', is_cover: false, target_canonical: null, is_favorite: false,
  });
  await page.route('**/api/gallery*', (r) =>
    r.fulfill({ json: [entry(0, 'aaaa'), entry(1, 'bbbb')] })
  );
  await page.route('**/api/projects/p1*', (r) => r.fulfill({ status: 404, body: '' }));
  await page.route('**/api/preview/aaaa/**', (r) => r.fulfill({ body: wideRed, contentType: 'image/png' }));
  await page.route('**/api/preview/bbbb/**', (r) => r.fulfill({ body: tallBlue, contentType: 'image/png' }));

  await page.goto('/compare?a=p1:0&b=p1:1');
  const slider = page.locator('[role="slider"][aria-label="Compare slider"]');
  await expect(slider).toBeVisible();
  // Both images loaded.
  await expect.poll(() =>
    slider.locator('img').evaluateAll((imgs) => imgs.every((i) => (i as HTMLImageElement).complete && (i as HTMLImageElement).naturalWidth > 0))
  ).toBe(true);

  // Structure: A is clipped to the left of the divider, B to the right.
  const clips = await slider.locator('img').evaluateAll((imgs) =>
    imgs.map((i) => ({ alt: (i as HTMLImageElement).alt, clip: getComputedStyle(i).clipPath }))
  );
  expect(clips[0].alt.startsWith('A')).toBe(true);
  expect(clips[0].clip).toMatch(/inset\(0(px)? 50% 0(px)? 0(px)?\)/);
  expect(clips[1].alt.startsWith('B')).toBe(true);
  expect(clips[1].clip).toMatch(/inset\(0(px)? 0(px)? 0(px)? 50%\)/);

  // Pixels: no blue (B) left of the divider, no red (A) right of it.
  const shot = await slider.screenshot();
  const counts = await page.evaluate(async (b64) => {
    const img = new Image();
    img.src = `data:image/png;base64,${b64}`;
    await img.decode();
    const c = document.createElement('canvas');
    c.width = img.width; c.height = img.height;
    const g = c.getContext('2d')!;
    g.drawImage(img, 0, 0);
    const d = g.getImageData(0, 0, c.width, c.height).data;
    const half = c.width / 2;
    let blueLeft = 0, redRight = 0, redLeft = 0, blueRight = 0;
    for (let y = 0; y < c.height; y += 2) {
      for (let x = 0; x < c.width; x += 2) {
        const k = (y * c.width + x) * 4;
        const r = d[k], gg = d[k + 1], b = d[k + 2];
        const isRed = r > 200 && gg < 60 && b < 60;
        const isBlue = b > 200 && r < 60 && gg < 60;
        if (x < half - 30) { if (isBlue) blueLeft++; if (isRed) redLeft++; }
        if (x > half + 30) { if (isRed) redRight++; if (isBlue) blueRight++; }
      }
    }
    return { blueLeft, redRight, redLeft, blueRight };
  }, shot.toString('base64'));
  expect(counts.redLeft).toBeGreaterThan(0); // A is on the left
  expect(counts.blueRight).toBeGreaterThan(0); // B is on the right
  expect(counts.blueLeft).toBe(0); // B never shows through on A's side
  expect(counts.redRight).toBe(0); // and A never on B's side
});
