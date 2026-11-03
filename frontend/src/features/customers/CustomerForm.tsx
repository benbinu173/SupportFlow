import { useState, type FormEvent } from 'react'

import { Button } from '@/components/ui/Button'
import { Field } from '@/components/ui/Field'
import { Input } from '@/components/ui/Input'
import type { components } from '@/types/api'

type Customer = components['schemas']['CustomerRead']

export interface CustomerFormValues {
  name: string
  email: string
  phone: string | null
  external_reference: string | null
}

export function CustomerForm({
  customer,
  saving,
  onSubmit,
  onCancel,
}: {
  customer?: Customer
  saving: boolean
  onSubmit: (values: CustomerFormValues) => void
  onCancel: () => void
}) {
  const [name, setName] = useState(customer?.name ?? '')
  const [email, setEmail] = useState(customer?.email ?? '')
  const [phone, setPhone] = useState(customer?.phone ?? '')
  const [externalReference, setExternalReference] = useState(customer?.external_reference ?? '')

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    onSubmit({
      name: name.trim(),
      email: email.trim(),
      phone: phone.trim() || null,
      external_reference: externalReference.trim() || null,
    })
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-4">
      <Field label="Customer name" required>
        <Input value={name} onChange={(event) => setName(event.target.value)} maxLength={200} autoFocus className="bg-surface" />
      </Field>
      <Field label="Email address" hint="Used to match correspondence and link a portal account." required>
        <Input type="email" value={email} onChange={(event) => setEmail(event.target.value)} maxLength={320} className="bg-surface" />
      </Field>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Phone" hint="Optional">
          <Input type="tel" value={phone} onChange={(event) => setPhone(event.target.value)} maxLength={50} className="bg-surface" />
        </Field>
        <Field label="External reference" hint="Your CRM or account ID">
          <Input value={externalReference} onChange={(event) => setExternalReference(event.target.value)} maxLength={200} className="bg-surface" />
        </Field>
      </div>
      <div className="flex justify-end gap-2 border-t border-border-subtle pt-4">
        <Button variant="ghost" onClick={onCancel} disabled={saving}>Cancel</Button>
        <Button type="submit" loading={saving} disabled={!name.trim() || !email.trim()}>
          {customer ? 'Save changes' : 'Add customer'}
        </Button>
      </div>
    </form>
  )
}
