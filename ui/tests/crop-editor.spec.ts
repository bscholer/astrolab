/**
 * The crop editor: draw, turn by the knob with snaps, and store what the
 * server says (on the sky). The framing API is mocked with a canvas whose
 * north points 30 degrees clockwise of up, so "north up" is a snap of its
 * own, apart from the 45-degree steps.
 */
import { test, expect, type Page } from '@playwright/test';

const W = 1200, H = 600, NORTH = 30;

type Box = { cx: number; cy: number; width: number; height: number; angle: number };

async function mock(page: Page) {
  const posted: Box[] = [];
  let stored: Box | null = null;
  const png = await page.evaluate(async ([w, h]) => {
    const c = new OffscreenCanvas(w, h);
    const g = c.getContext('2d')!;
    g.fillStyle = '#203040';
    g.fillRect(0, 0, w, h);
    const buf = new Uint8Array(await (await c.convertToBlob({ type: 'image/png' })).arrayBuffer());
    let s = '';
    for (const b of buf) s += String.fromCharCode(b);
    return btoa(s);
  }, [W / 4, H / 4]);
  await page.route('**/api/preview/h1/**', (r) =>
    r.fulfill({ body: Buffer.from(png, 'base64'), contentType: 'image/png' })
  );
  await page.route('**/api/framing/h1/image/box', async (r) => {
    const box = r.request().postDataJSON() as Box;
    posted.push(box);
    stored = box;
    // The server stores it on the sky; the editor just keeps what comes back.
    await r.fulfill({ json: { ra: 83.8, dec: 5.0, sky_width: box.width * 0.046, sky_height: box.height * 0.046, angle: box.angle } });
  });
  await page.route('**/api/framing/h1/image', (r) =>
    r.fulfill({
      json: { width: W, height: H, solved: true, north_angle: NORTH, arcsec_per_px: 2.78, box: stored }
    })
  );
  return posted;
}

test('draw a box, turn it with snaps, store it on the sky', async ({ page }) => {
  await page.goto('/');
  const posted = await mock(page);
  await page.goto('/playground/crop?hash=h1');
  const svg = page.locator('svg[aria-label="Crop picker"]');
  await expect(svg).toBeVisible();
  const r = (await svg.boundingBox())!;
  // The overlay keeps the canvas's shape (2:1), so pixels map straight through.
  expect(r.width / r.height).toBeCloseTo(W / H, 1);
  const at = (x: number, y: number) => ({ x: r.x + (x / W) * r.width, y: r.y + (y / H) * r.height });

  // Draw from (300,150) to (900,450): a 600 x 300 box centred on the canvas.
  let p = at(300, 150);
  await page.mouse.move(p.x, p.y);
  await page.mouse.down();
  p = at(900, 450);
  await page.mouse.move(p.x, p.y, { steps: 5 });
  await page.mouse.up();
  await expect.poll(() => posted.length).toBe(1);
  expect(posted[0].cx).toBeCloseTo(600, -1);
  expect(posted[0].cy).toBeCloseTo(300, -1);
  expect(posted[0].width).toBeCloseTo(600, -1);
  expect(posted[0].angle).toBe(0);
  await expect(page.getByTestId('stored')).toContainText('sky_width');

  // Turn by the knob to about 32 degrees: snaps to north up (30).
  const knob = page.locator('circle[aria-label="Turn the crop"]');
  const kb = (await knob.boundingBox())!;
  const centre = at(600, 300);
  const turnTo = async (deg: number) => {
    const a = (deg * Math.PI) / 180, len = 200;
    await page.mouse.move(kb.x + kb.width / 2, kb.y + kb.height / 2);
    await page.mouse.down();
    await page.mouse.move(centre.x + len * Math.sin(a), centre.y - len * Math.cos(a), { steps: 6 });
    await page.mouse.up();
  };
  await turnTo(32);
  await expect.poll(() => posted.length).toBe(2);
  expect(posted[1].angle).toBeCloseTo(NORTH, 5);
  await expect(page.locator('.facing')).toHaveText('north up');

  // The rotated knob sits elsewhere now; use the buttons for a quarter turn.
  await page.getByRole('button', { name: '⟳ 90°' }).click();
  await expect.poll(() => posted.length).toBe(3);
  expect(posted[2].angle).toBeCloseTo(NORTH + 90, 5);
  await expect(page.locator('.facing')).toHaveText('west up');

  await page.getByRole('button', { name: 'North up' }).click();
  await expect.poll(() => posted.length).toBe(4);
  expect(posted[3].angle).toBeCloseTo(NORTH, 5);

  // Typed angles are free, no snapping.
  const input = page.locator('.angle input');
  await input.fill('47');
  await input.press('Enter');
  await expect.poll(() => posted.length).toBe(5);
  expect(posted[4].angle).toBeCloseTo(47, 5);
});
