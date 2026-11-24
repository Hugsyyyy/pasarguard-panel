// Pengu fork: traffic baskets. In the admin dialog the data limit field is the overall volume (good for
// every service) and sits in one box with a volume field per basket (e.g. Xray / WireGuard), saved with
// the dialog. The admins list shows each basket's usage under the data usage bar. The owner defines the
// baskets under Settings (pages/_dashboard.settings.baskets.tsx).
import UsageSliderCompact from '@/components/common/usage-slider-compact'
import { DecimalInput } from '@/components/common/decimal-input'
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { getGroupsSimple } from '@/service/api'
import { fetcher } from '@/service/http'
import { bytesToFormGigabytes, formatBytes, gbToBytes } from '@/utils/formatByte'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Layers, Pencil, Plus, Save, Trash2, X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

type Basket = { id: number; name: string; sort_order: number; group_ids: number[] }
type BasketsResponse = { baskets: Basket[]; ambiguous_node_ids: number[] }
type Volume = { data_limit: number | null; used_traffic: number; remaining: number | null; blocked: boolean }
type AdminBasket = Volume & { basket_id: number; name: string; group_ids: number[] }
type AdminBaskets = { username: string; basket_mode: boolean; shared: Volume | null; baskets: AdminBasket[] }
type Overview = { baskets: Basket[]; admins: AdminBaskets[] }

const errorText = (error: any) => error?.data?.detail || error?.response?._data?.detail || error?.message || String(error)
const adminBase = (username: string) => `/api/admin/by-username/${encodeURIComponent(username)}/baskets`

function Ltr({ children }: { children: React.ReactNode }) {
  return (
    <span dir="ltr" style={{ unicodeBidi: 'isolate' }}>
      {children}
    </span>
  )
}

export function useBasketDefinitions(enabled = true) {
  return useQuery({ queryKey: ['traffic-baskets'], queryFn: () => fetcher<BasketsResponse>('/api/pengu/baskets'), enabled, retry: false, staleTime: 60_000 })
}

/** Every admin in basket mode, for the admins list (one call for the whole table). */
export function useBasketOverview() {
  return useQuery({
    queryKey: ['traffic-baskets-overview'],
    queryFn: () => fetcher<Overview>('/api/pengu/baskets/admins'),
    retry: false,
    staleTime: 30_000,
  })
}

const invalidateBasketQueries = (client: ReturnType<typeof useQueryClient>) =>
  Promise.all([
    client.invalidateQueries({ queryKey: ['traffic-baskets-overview'] }),
    client.invalidateQueries({ queryKey: ['traffic-baskets-me'] }),
    client.invalidateQueries({ queryKey: ['traffic-baskets-admin'] }),
    client.invalidateQueries({ queryKey: ['/api/admins'] }),
  ])

// ---------------------------------------------------------------- admin dialog

export type AdminBasketsState = ReturnType<typeof useAdminBaskets>

/**
 * Per-basket volumes of the admin being edited (or created), kept as a draft until the dialog is saved.
 * The dialog's own data limit field is the overall (shared) volume: for an admin in basket mode the
 * panel stores a sent data limit as its shared volume. `onShared` puts that value into the form.
 * Giving any basket a volume puts the admin in basket mode (its data limit becomes the shared volume,
 * so nothing changes for its users); with every basket at 0 the two modes behave the same.
 */
export function useAdminBaskets({ open, username, canManage, onShared }: { open: boolean; username: string | null; canManage: boolean; onShared: (bytes: number | null) => void }) {
  const client = useQueryClient()
  const definitions = useBasketDefinitions(open && canManage)
  const current = useQuery({
    queryKey: ['traffic-baskets-admin', username],
    queryFn: () => fetcher<AdminBaskets>(adminBase(username!)),
    enabled: open && canManage && !!username,
    retry: false,
  })
  const [draft, setDraft] = useState<Record<number, number> | null>(null)
  const loadedFor = useRef<string | null>(null)
  const onSharedRef = useRef(onShared)
  onSharedRef.current = onShared

  // (Re)start the draft from the server state each time the dialog opens for an admin.
  useEffect(() => {
    if (!open) {
      loadedFor.current = null
      setDraft(null)
      return
    }
    const key = username ?? '<new>'
    if (loadedFor.current === key) return
    if (username && !current.data) return
    loadedFor.current = key
    const data = current.data
    if (data?.basket_mode) onSharedRef.current(data.shared?.data_limit ?? null)
    setDraft(Object.fromEntries((data?.baskets ?? []).map(b => [b.basket_id, b.data_limit ?? 0])))
  }, [open, username, current.data])

  const baskets = definitions.data?.baskets ?? []
  const visible = canManage && baskets.length > 0 && !!draft

  const setBasket = useCallback((id: number, bytes: number) => setDraft(d => ({ ...(d ?? {}), [id]: bytes })), [])

  const apply = useCallback(
    async (target: string) => {
      if (!visible || !draft) return
      let state = username ? current.data : undefined
      const base = adminBase(target)
      const wanted = baskets.map(b => ({ id: b.id, want: draft[b.id] ?? 0, have: state?.baskets.find(x => x.basket_id === b.id)?.data_limit ?? 0 }))
      const changes = wanted.filter(w => w.want !== w.have)
      if (!changes.length) return
      if (!state?.basket_mode) {
        if (!changes.some(w => w.want > 0)) return
        state = await fetcher<AdminBaskets>(`${base}/enable`, { method: 'POST' })
      }
      for (const w of changes) await fetcher(`${base}/${w.id}`, { method: 'PUT', body: { data_limit: w.want } })
      await invalidateBasketQueries(client)
    },
    [visible, draft, username, current.data, baskets, client],
  )

  return {
    visible,
    loading: open && canManage && (definitions.isLoading || (!!username && current.isLoading)),
    definitionsLoaded: !!definitions.data,
    baskets,
    current: current.data,
    draft,
    setBasket,
    apply,
  }
}

function GbField({ label, value, onChange, used }: { label: string; value: number; onChange: (bytes: number) => void; used?: number }) {
  const { t } = useTranslation()
  return (
    <div className="flex flex-col gap-2">
      <span className="text-sm font-medium">{label}</span>
      <div className="relative">
        <DecimalInput
          placeholder="0"
          value={value ? bytesToFormGigabytes(value) : null}
          onValueChange={v => onChange(v == null ? 0 : (gbToBytes(v) ?? 0))}
          emptyValue={undefined}
          className="pr-10"
        />
        <span className="text-muted-foreground pointer-events-none absolute top-1/2 right-3 -translate-y-1/2 text-xs font-medium">{t('userDialog.gb', { defaultValue: 'GB' })}</span>
      </div>
      {used != null && used > 0 && (
        <p className="text-muted-foreground text-[11px]">
          {t('trafficBaskets.used', { defaultValue: 'Used' })}: <Ltr>{formatBytes(used)}</Ltr>
        </p>
      )}
    </div>
  )
}

/**
 * Replaces the admin dialog's data limit field when baskets exist: one box with the data limit
 * (the overall volume, passed in as `dataLimitField`) and one volume field per basket under it.
 */
export function AdminBasketFields({ state, dataLimitField }: { state: AdminBasketsState; dataLimitField: React.ReactNode }) {
  const { t } = useTranslation()
  if (!state.visible || !state.draft) return <>{dataLimitField}</>
  const { draft, current } = state
  const usedOf = (id: number) => current?.baskets.find(b => b.basket_id === id)?.used_traffic
  return (
    <div className="space-y-3 rounded-md border p-3 sm:col-span-2">
      <span className="flex items-center gap-2 text-sm font-medium">
        <Layers className="h-4 w-4" />
        {t('trafficBaskets.volumes', { defaultValue: 'Volumes' })}
      </span>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="sm:col-span-2">{dataLimitField}</div>
        {state.baskets.map(b => (
          <GbField key={b.id} label={b.name} value={draft[b.id] ?? 0} onChange={v => state.setBasket(b.id, v)} used={current?.basket_mode ? usedOf(b.id) : undefined} />
        ))}
      </div>
      <p className="text-muted-foreground text-xs leading-5">
        {t('trafficBaskets.perServiceHint', {
          defaultValue: 'Data limit is the overall volume, good for every service. Each service uses its own volume first, then the overall one; when both run out only that service stops.',
        })}
      </p>
    </div>
  )
}

// ---------------------------------------------------------------- admins list

/**
 * Data usage cell. For an admin in basket mode the bar is its overall (shared) volume, as in the dialog,
 * and each basket with a volume gets a small line under it.
 */
export function AdminUsageCell({
  username,
  dataLimit,
  used,
  lifetimeUsed,
  status,
  isMobile,
}: {
  username: string
  dataLimit: number | null | undefined
  used: number
  lifetimeUsed?: number
  status: string
  isMobile?: boolean
}) {
  const { data } = useBasketOverview()
  const entry = data?.admins?.find(a => a.username === username)
  const inBaskets = !!entry?.basket_mode && !!entry.shared
  const lines = inBaskets ? entry!.baskets.filter(b => (b.data_limit ?? 0) > 0 || b.used_traffic > 0) : []
  return (
    <div className="flex w-full flex-col">
      <UsageSliderCompact
        isMobile={isMobile}
        total={inBaskets ? entry!.shared!.data_limit : dataLimit}
        used={inBaskets ? entry!.shared!.used_traffic : used}
        totalUsedTraffic={lifetimeUsed}
        status={status}
      />
      {lines.length > 0 && (
        <div className="text-muted-foreground mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] leading-4">
          {lines.map(b => (
            <span key={b.basket_id} className={b.blocked ? 'text-destructive' : undefined}>
              {b.name}: <Ltr>{`${formatBytes(b.used_traffic)} / ${formatBytes(b.data_limit ?? 0)}`}</Ltr>
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- basket definitions (owner)

function BasketEditor({ groups, initial, onDone }: { groups: { id: number; name: string }[]; initial?: Basket; onDone: () => void }) {
  const { t } = useTranslation()
  const client = useQueryClient()
  const [name, setName] = useState(initial?.name ?? '')
  const [selected, setSelected] = useState<number[]>(initial?.group_ids ?? [])
  const [busy, setBusy] = useState(false)
  const save = async () => {
    setBusy(true)
    try {
      const body = { name: name.trim(), group_ids: selected }
      if (initial) await fetcher(`/api/pengu/baskets/${initial.id}`, { method: 'PUT', body })
      else await fetcher('/api/pengu/baskets', { method: 'POST', body })
      await client.invalidateQueries({ queryKey: ['traffic-baskets'] })
      await invalidateBasketQueries(client)
      toast.success(t('trafficBaskets.saved', { defaultValue: 'Saved' }))
      onDone()
    } catch (error) {
      toast.error(t('trafficBaskets.saveFailed', { defaultValue: 'Could not save' }), { description: errorText(error) })
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="space-y-3 rounded-md border p-3">
      <Input value={name} onChange={e => setName(e.target.value)} maxLength={64} placeholder={t('trafficBaskets.namePlaceholder', { defaultValue: 'Basket name, e.g. WireGuard' })} />
      <div className="grid gap-2 sm:grid-cols-2">
        {groups.map(g => (
          <label key={g.id} className="flex cursor-pointer items-center gap-2 text-sm">
            <Checkbox checked={selected.includes(g.id)} onCheckedChange={v => setSelected(s => (v ? [...s, g.id] : s.filter(x => x !== g.id)))} />
            <span>{g.name}</span>
          </label>
        ))}
      </div>
      <div className="flex gap-2">
        <Button type="button" size="sm" onClick={save} disabled={busy || !name.trim()}>
          <Save className="h-4 w-4" />
          {t('trafficBaskets.save', { defaultValue: 'Save' })}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onDone} disabled={busy}>
          <X className="h-4 w-4" />
          {t('cancel', { defaultValue: 'Cancel' })}
        </Button>
      </div>
    </div>
  )
}

/** Settings → Volume categories (owner): which groups make up each basket. */
export function BasketDefinitionsPanel() {
  const { t } = useTranslation()
  const client = useQueryClient()
  const { data, isLoading } = useBasketDefinitions()
  const { data: groupData } = useQuery({ queryKey: ['groups-simple-baskets'], queryFn: () => getGroupsSimple({ all: true }) })
  const groups = groupData?.groups ?? []
  const groupName = useMemo(() => new Map(groups.map(g => [g.id, g.name])), [groups])
  const [editing, setEditing] = useState<number | 'new' | null>(null)
  const [deleting, setDeleting] = useState<Basket | null>(null)

  const remove = async () => {
    if (!deleting) return
    try {
      await fetcher(`/api/pengu/baskets/${deleting.id}`, { method: 'DELETE' })
      await client.invalidateQueries({ queryKey: ['traffic-baskets'] })
      await invalidateBasketQueries(client)
    } catch (error) {
      toast.error(t('trafficBaskets.saveFailed', { defaultValue: 'Could not save' }), { description: errorText(error) })
    } finally {
      setDeleting(null)
    }
  }

  return (
    <div className="space-y-4">
        <p className="text-muted-foreground text-sm leading-6">
          {t('trafficBaskets.definitionsHint', {
            defaultValue:
              'A basket is a set of groups. Usage on a node is charged to the basket of the groups it serves: first the admin’s own volume in that basket, then its shared volume. When both are empty, only that basket’s configs stop working for the admin’s users.',
          })}
        </p>
        {isLoading ? (
          <Skeleton className="h-24" />
        ) : (
          <div className="space-y-3">
            {!!data?.ambiguous_node_ids?.length && (
              <div className="flex items-start gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
                <span>
                  {t('trafficBaskets.ambiguous', { defaultValue: 'These nodes serve groups of more than one basket, so their usage is charged to the shared volume:' })}{' '}
                  <Ltr>{data.ambiguous_node_ids.join(', ')}</Ltr>
                </span>
              </div>
            )}
            <div className="grid gap-3 md:grid-cols-2">
              {data?.baskets.map(b =>
                editing === b.id ? (
                  <BasketEditor key={b.id} groups={groups} initial={b} onDone={() => setEditing(null)} />
                ) : (
                  <div key={b.id} className="space-y-2 rounded-md border p-3">
                    <div className="flex items-center justify-between gap-2">
                      <span className="flex items-center gap-2 font-medium">
                        <Layers className="text-primary h-4 w-4" />
                        {b.name}
                      </span>
                      <div className="flex gap-1">
                        <Button type="button" size="icon" variant="ghost" className="h-8 w-8" onClick={() => setEditing(b.id)} aria-label={t('edit', { defaultValue: 'Edit' })}>
                          <Pencil className="h-4 w-4" />
                        </Button>
                        <Button type="button" size="icon" variant="ghost" className="text-destructive h-8 w-8" onClick={() => setDeleting(b)} aria-label={t('delete', { defaultValue: 'Delete' })}>
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {b.group_ids.length ? (
                        b.group_ids.map(id => (
                          <Badge key={id} variant="secondary">
                            {groupName.get(id) ?? `#${id}`}
                          </Badge>
                        ))
                      ) : (
                        <span className="text-muted-foreground text-xs">{t('trafficBaskets.noGroups', { defaultValue: 'No groups yet' })}</span>
                      )}
                    </div>
                  </div>
                ),
              )}
              {editing === 'new' ? (
                <BasketEditor groups={groups} onDone={() => setEditing(null)} />
              ) : (
                <Button type="button" variant="outline" className="h-auto min-h-16 border-dashed" onClick={() => setEditing('new')}>
                  <Plus className="h-4 w-4" />
                  {t('trafficBaskets.newBasket', { defaultValue: 'New basket' })}
                </Button>
              )}
            </div>
          </div>
        )}
        <AlertDialog open={!!deleting} onOpenChange={o => !o && setDeleting(null)}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>{t('trafficBaskets.deleteTitle', { defaultValue: 'Delete this basket?' })}</AlertDialogTitle>
              <AlertDialogDescription>
                {t('trafficBaskets.deleteHint', { defaultValue: "Every admin's volume in this basket is deleted with it; its groups are then covered by the shared volume only." })}
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>{t('cancel', { defaultValue: 'Cancel' })}</AlertDialogCancel>
              <AlertDialogAction onClick={remove}>{t('delete', { defaultValue: 'Delete' })}</AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
    </div>
  )
}
