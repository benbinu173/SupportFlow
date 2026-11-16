import { readFile } from 'node:fs/promises'

import { expect, now, test, ticketId } from './fixtures'

test('a customer uploads multipart file bytes and downloads the attachment', async ({ page, api }) => {
  const content = Buffer.from('Delivery confirmed for Friday.\n')
  const filename = 'delivery.txt'
  const attachment = {
    id: '00000000-0000-4000-8000-000000000201', ticket_id: ticketId,
    message_id: null, uploaded_by_id: '00000000-0000-4000-8000-000000000004',
    filename, content_type: 'text/plain', size_bytes: content.length, created_at: now,
  }
  let uploaded = false

  await api.openAs('customer', `/tickets/${ticketId}`)
  await page.route(`**/api/v1/tickets/${ticketId}/attachments?*`, (route) =>
    route.fulfill({ json: uploaded ? [attachment] : [] }),
  )
  await page.route(`**/api/v1/tickets/${ticketId}/attachments`, async (route) => {
    const request = route.request()
    if (request.method() === 'GET') return route.fulfill({ json: uploaded ? [attachment] : [] })

    expect(request.method()).toBe('POST')
    const contentType = await request.headerValue('content-type')
    expect(contentType).toMatch(/^multipart\/form-data; boundary=/)
    const body = request.postDataBuffer()
    expect(body).not.toBeNull()
    const multipart = await new Response(new Uint8Array(body!), {
      headers: { 'Content-Type': contentType! },
    }).formData()
    const file = multipart.get('file') as File
    expect(file.name).toBe(filename)
    expect(file.type).toBe('text/plain')
    expect(Buffer.from(await file.arrayBuffer())).toEqual(content)
    uploaded = true
    await route.fulfill({ status: 201, json: attachment })
  })
  await page.route(`**/api/v1/attachments/${attachment.id}`, (route) => route.fulfill({
    status: 200, body: content,
    headers: {
      'Content-Type': 'text/plain',
      'Content-Disposition': `attachment; filename="${filename}"`,
      'X-Content-Type-Options': 'nosniff',
    },
  }))

  await page.getByLabel('Choose an attachment', { exact: true }).setInputFiles({
    name: filename, mimeType: 'text/plain', buffer: content,
  })
  const downloadButton = page.getByRole('button', { name: `Download ${filename}`, exact: true })
  await expect(downloadButton).toBeVisible()
  const incoming = page.waitForEvent('download')
  await downloadButton.click()
  const download = await incoming
  expect(download.suggestedFilename()).toBe(filename)
  expect(await readFile((await download.path())!)).toEqual(content)
})
