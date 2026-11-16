import { accounts, expect, now, test } from './fixtures'

test.describe('knowledge usage analytics', () => {
  test.skip(process.env.VITE_KNOWLEDGE_ENABLED !== 'true', 'Knowledge is deferred to v2')
  for (const outcome of ['answer', 'failure'] as const) {
    test(`a knowledge ${outcome} refreshes previously viewed analytics`, async ({ page, api }) => {
      api.knowledgeDocuments.push({
        id: '00000000-0000-4000-8000-000000000200', title: 'Refund policy', source_type: 'manual',
        status: 'completed', is_published: true, chunk_count: 1, error_message: null,
        created_by_id: accounts.admin.id, created_at: now, updated_at: now, processed_at: now,
      })
      await api.openAs('admin', '/analytics')
      const summary = page.getByRole('region', { name: 'Performance summary' })
      await expect(summary.getByText('0 calls · 0 failed', { exact: true })).toBeVisible()

      await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Knowledge base' }).click()
      api.failNextKnowledgeAnswer = outcome === 'failure'
      await page.getByRole('textbox', { name: 'Ask a question' }).fill('How long do refunds take?')
      await page.getByRole('button', { name: 'Ask', exact: true }).click()
      if (outcome === 'answer') {
        await expect(page.getByText('Refunds take five working days.', { exact: true })).toBeVisible()
      } else {
        await expect(page.getByRole('alert').getByText('That question couldn’t be answered', { exact: true })).toBeVisible()
      }

      await page.getByRole('link', { name: 'View insights' }).click()
      await expect(summary.getByText(`1 calls · ${outcome === 'failure' ? 1 : 0} failed`, { exact: true })).toBeVisible()
      await expect(page.getByText('Knowledge answers', { exact: true })).toBeVisible()
    })
  }

  for (const outcome of ['completed', 'failed'] as const) {
    test(`${outcome} document processing refreshes previously viewed analytics`, async ({ page, api }) => {
      await api.openAs('admin', '/analytics')
      const summary = page.getByRole('region', { name: 'Performance summary' })
      await expect(summary.getByText('0 calls · 0 failed', { exact: true })).toBeVisible()

      await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Knowledge base' }).click()
      await page.getByRole('button', { name: 'Add source' }).click()
      const form = page.getByRole('dialog', { name: 'Add to your knowledge base' })
      await form.getByRole('textbox', { name: 'Document title' }).fill('Browser check: refunds')
      await form.getByRole('textbox', { name: 'Content' }).fill('Refunds take five working days.')
      await form.getByRole('button', { name: 'Add document' }).click()
      await expect(form).toHaveCount(0)

      const source = page.getByRole('article').filter({ has: page.getByRole('heading', { name: 'Browser check: refunds' }) })
      await expect(source.getByText('Queued', { exact: true })).toBeVisible()
      expect(api.knowledgeDocuments).toHaveLength(1)
      api.finishIngestion(api.knowledgeDocuments[0]!.id, outcome)
      await expect(source.getByText(outcome === 'completed' ? 'Ready' : 'Failed', { exact: true })).toBeVisible()

      await page.getByRole('link', { name: 'View insights' }).click()
      await expect(summary.getByText(`1 calls · ${outcome === 'failed' ? 1 : 0} failed`, { exact: true })).toBeVisible()
      await expect(page.getByText('Knowledge indexing and search', { exact: true })).toBeVisible()
    })
  }
})
