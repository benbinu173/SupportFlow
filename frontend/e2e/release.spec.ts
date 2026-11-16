import { expect, test } from './fixtures'

test('v1 hides knowledge navigation and direct URLs', async ({ page, api }) => {
  test.skip(process.env.VITE_KNOWLEDGE_ENABLED === 'true', 'Checks the v1 release')
  await api.openAs('admin', '/dashboard')
  await expect(page.getByRole('navigation', { name: 'Main' })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Knowledge base' })).toHaveCount(0)
  await page.goto('/knowledge')
  await expect(page.getByRole('heading', { name: 'Page not found' })).toBeVisible()
})
