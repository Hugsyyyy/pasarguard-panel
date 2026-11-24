// Pengu fork: traffic baskets — separate volume per set of groups (e.g. Xray / WireGuard) plus a shared volume.
// The owner defines the baskets and sets each admin's volumes; every admin sees its own.
import PageHeader from '@/components/layout/page-header'
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Progress } from '@/components/ui/progress'
import { Separator } from '@/components/ui/separator'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { useAdmin } from '@/hooks/use-admin'
import { fetcher } from '@/service/http'
import { getAdminsSimple, getGroupsSimple } from '@/service/api'
import { formatBytes } from '@/utils/formatByte'
import { hasPermission, isOwner } from '@/utils/rbac'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Layers, Pencil, Plus, Save, Search, Trash2, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

const GB = 1024 ** 3

type Basket = { id: number; name: string; sort_order: number; group_ids: number[] }
type BasketsResponse = { baskets: Basket[]; ambiguous_node_ids: number[] }
type Volume = { data_limit: number | null; used_traffic: number; remaining: number | null; blocked: boolean }
type AdminBasket = Volume & { basket_id: number; name: string; group_ids: number[] }
type AdminBaskets = { username: string; basket_mode: boolean; shared: Volume | null; baskets: AdminBasket[] }

const toGb = (bytes: number | null | undefined) => (bytes == null ? '' : String(Math.round((bytes / GB) * 100) / 100))
const fromGb = (value: string) => Math.max(0, Math.round(parseFloat(value || '0') * GB))
const errorText = (error: any) => error?.data?.detail || error?.message || String(error)

function Ltr({ children }: { children: React.ReactNode }) {
  return (
    <span dir="ltr" style={{ unicodeBidi: 'isolate' }}>
      {children}
    </span>
  )
}

function VolumeBar({ title, volume }: { title: string; volume: Volume }) {
  const { t } = useTranslation()
  const unlimited = volume.data_limit == null
  const pct = !unlimited && volume.data_limit! > 0 ? Math.min(100, (volume.used_traffic / volume.data_limit!) * 100) : volume.blocked ? 100 : 0
  return (
    <div className="space-y-1.5 rounded-md border p-3">
      <div className="flex items-center justify-between gap-2 text-sm">
        <span className="font-medium">{title}</span>
        {volume.blocked ? (
          <Badge variant="destructive">{t('trafficBaskets.usedUp', { defaultValue: 'Used up' })}</Badge>
        ) : (
          <Badge variant="outline">{t('trafficBaskets.active', { defaultValue: 'Active' })}</Badge>
        )}
      </div>
      <div className="text-muted-foreground flex items-center justify-between text-xs">
        <span>
          {t('trafficBaskets.used', { defaultValue: 'Used' })}: <Ltr>{formatBytes(volume.used_traffic)}</Ltr>
        </span>
        <span>
          {t('trafficBaskets.remaining', { defaultValue: 'Remaining' })}: <Ltr>{unlimited ? '∞' : formatBytes(volume.remaining ?? 0)}</Ltr>
          {!unlimited && (
            <>
              {' '}
              / <Ltr>{formatBytes(volume.data_limit ?? 0)}</Ltr>
            </>
          )}
        </span>
      </div>
      {!unlimited && <Progress value={pct} className="h-1.5" indicatorClassName={volume.blocked ? 'bg-destructive' : undefined} />}
    </div>
  )
}

function MyVolumes() {
  const { t } = useTranslation()
  const { data, isLoading } = useQuery({ queryKey: ['traffic-baskets-me'], queryFn: () => fetcher<AdminBaskets>('/api/pengu/baskets/me'), retry: false })
  if (isLoading) return <Skeleton className="h-24" />
  if (!data?.basket_mode) {
    return <p className="text-muted-foreground text-sm">{t('trafficBaskets.notInBasketMode', { defaultValue: 'Your panel uses one volume for every service.' })}</p>
  }
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {data.baskets.map(b => (
        <VolumeBar key={b.basket_id} title={b.name} volume={b} />
      ))}
      {data.shared && (data.shared.data_limit == null || data.shared.data_limit > 0) && (
        <VolumeBar title={t('trafficBaskets.shared', { defaultValue: 'Shared (every service)' })} volume={data.shared} />
      )}
    </div>
  )
}

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
        <Button size="sm" onClick={save} disabled={busy || !name.trim()}>
          <Save className="h-4 w-4" />
          {t('trafficBaskets.save', { defaultValue: 'Save' })}
        </Button>
        <Button size="sm" variant="ghost" onClick={onDone} disabled={busy}>
          <X className="h-4 w-4" />
          {t('cancel', { defaultValue: 'Cancel' })}
        </Button>
      </div>
    </div>
  )
}

function BasketDefinitions() {
  const { t } = useTranslation()
  const client = useQueryClient()
  const { data, isLoading } = useQuery({ queryKey: ['traffic-baskets'], queryFn: () => fetcher<BasketsResponse>('/api/pengu/baskets') })
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
    } catch (error) {
      toast.error(t('trafficBaskets.saveFailed', { defaultValue: 'Could not save' }), { description: errorText(error) })
    } finally {
      setDeleting(null)
    }
  }

  if (isLoading) return <Skeleton className="h-24" />
  return (
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
                  <Button size="icon" variant="ghost" className="h-8 w-8" onClick={() => setEditing(b.id)} aria-label={t('edit', { defaultValue: 'Edit' })}>
                    <Pencil className="h-4 w-4" />
                  </Button>
                  <Button size="icon" variant="ghost" className="text-destructive h-8 w-8" onClick={() => setDeleting(b)} aria-label={t('delete', { defaultValue: 'Delete' })}>
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
          <Button variant="outline" className="h-auto min-h-16 border-dashed" onClick={() => setEditing('new')}>
            <Plus className="h-4 w-4" />
            {t('trafficBaskets.newBasket', { defaultValue: 'New basket' })}
          </Button>
        )}
      </div>
      <AlertDialog open={!!deleting} onOpenChange={open => !open && setDeleting(null)}>
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

function VolumeRow({
  title,
  volume,
  allowUnlimited,
  onSet,
  onAdd,
}: {
  title: string
  volume: Volume
  allowUnlimited?: boolean
  onSet: (bytes: number | null) => Promise<void>
  onAdd: (bytes: number) => Promise<void>
}) {
  const { t } = useTranslation()
  const [limit, setLimit] = useState(toGb(volume.data_limit))
  const [unlimited, setUnlimited] = useState(volume.data_limit == null)
  const [add, setAdd] = useState('')
  const [busy, setBusy] = useState(false)
  const run = async (fn: () => Promise<void>) => {
    setBusy(true)
    try {
      await fn()
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="space-y-2">
      <VolumeBar title={title} volume={volume} />
      <div className="flex flex-wrap items-center gap-2">
        {allowUnlimited && (
          <label className="flex items-center gap-2 text-xs">
            <Switch checked={unlimited} onCheckedChange={setUnlimited} />
            {t('trafficBaskets.unlimited', { defaultValue: 'Unlimited' })}
          </label>
        )}
        {!unlimited && (
          <Input className="h-8 w-28" inputMode="decimal" dir="ltr" value={limit} onChange={e => setLimit(e.target.value)} aria-label={t('trafficBaskets.limitGb', { defaultValue: 'Limit (GB)' })} />
        )}
        <Button size="sm" variant="secondary" disabled={busy} onClick={() => run(() => onSet(unlimited ? null : fromGb(limit)))}>
          {t('trafficBaskets.setLimit', { defaultValue: 'Set limit (GB)' })}
        </Button>
        {!(allowUnlimited && volume.data_limit == null) && (
          <>
            <Input
              className="h-8 w-24"
              inputMode="decimal"
              dir="ltr"
              value={add}
              onChange={e => setAdd(e.target.value)}
              placeholder="GB"
              aria-label={t('trafficBaskets.addGb', { defaultValue: 'Add (GB)' })}
            />
            <Button
              size="sm"
              disabled={busy || !add}
              onClick={() =>
                run(async () => {
                  await onAdd(Math.round(parseFloat(add) * GB))
                  setAdd('')
                })
              }
            >
              <Plus className="h-4 w-4" />
              {t('trafficBaskets.add', { defaultValue: 'Add' })}
            </Button>
          </>
        )}
      </div>
    </div>
  )
}

function AdminVolumes() {
  const { t } = useTranslation()
  const [search, setSearch] = useState('')
  const [username, setUsername] = useState<string | null>(null)
  const { data: admins } = useQuery({ queryKey: ['admins-simple-baskets', search], queryFn: () => getAdminsSimple({ search: search || undefined, limit: 20 }) })
  const query = useQuery({
    queryKey: ['traffic-baskets-admin', username],
    queryFn: () => fetcher<AdminBaskets>(`/api/admin/by-username/${encodeURIComponent(username!)}/baskets`),
    enabled: !!username,
  })
  const base = username ? `/api/admin/by-username/${encodeURIComponent(username)}/baskets` : ''
  const call = async (url: string, method: 'POST' | 'PUT', body?: object) => {
    try {
      const result = await fetcher<AdminBaskets>(url, { method, body })
      query.refetch()
      toast.success(t('trafficBaskets.saved', { defaultValue: 'Saved' }))
      return result
    } catch (error) {
      toast.error(t('trafficBaskets.saveFailed', { defaultValue: 'Could not save' }), { description: errorText(error) })
    }
  }
  const data = query.data
  return (
    <div className="space-y-3">
      <div className="relative max-w-sm">
        <Search className="text-muted-foreground absolute start-2 top-2.5 h-4 w-4" />
        <Input className="ps-8" value={search} onChange={e => setSearch(e.target.value)} placeholder={t('trafficBaskets.searchAdmin', { defaultValue: 'Search an admin' })} />
      </div>
      <div className="flex flex-wrap gap-1">
        {admins?.admins.map(a => (
          <Button key={a.id} size="sm" variant={a.username === username ? 'default' : 'outline'} onClick={() => setUsername(a.username)}>
            {a.username}
          </Button>
        ))}
      </div>
      {username && query.isLoading && <Skeleton className="h-24" />}
      {username && data && !data.basket_mode && (
        <div className="space-y-2 rounded-md border p-3 text-sm">
          <p>
            {t('trafficBaskets.enableHint', {
              defaultValue: 'This admin uses one volume for every service. Turning baskets on keeps that volume as its shared volume; then you can give it volume per basket.',
            })}
          </p>
          <Button size="sm" onClick={() => call(`${base}/enable`, 'POST')}>
            {t('trafficBaskets.enable', { defaultValue: 'Turn on baskets' })}
          </Button>
        </div>
      )}
      {username && data?.basket_mode && (
        <div className="grid gap-4 lg:grid-cols-2">
          {data.baskets.map(b => (
            <VolumeRow
              key={`${username}-${b.basket_id}-${b.data_limit}`}
              title={b.name}
              volume={b}
              onSet={async bytes => {
                await call(`${base}/${b.basket_id}`, 'PUT', { data_limit: bytes ?? 0 })
              }}
              onAdd={async bytes => {
                await call(`${base}/${b.basket_id}/add`, 'POST', { bytes })
              }}
            />
          ))}
          {data.shared && (
            <VolumeRow
              key={`${username}-shared-${data.shared.data_limit}`}
              title={t('trafficBaskets.shared', { defaultValue: 'Shared (every service)' })}
              volume={data.shared}
              allowUnlimited
              onSet={async bytes => {
                await call(`${base}/shared`, 'PUT', { data_limit: bytes })
              }}
              onAdd={async bytes => {
                await call(`${base}/shared/add`, 'POST', { bytes })
              }}
            />
          )}
        </div>
      )}
    </div>
  )
}

export default function TrafficBasketsPage() {
  const { t } = useTranslation()
  const { admin } = useAdmin()
  const owner = isOwner(admin)
  const canManage = owner || hasPermission(admin, 'admins', 'update')
  return (
    <div className="flex w-full flex-col items-start gap-2">
      <PageHeader title="trafficBaskets.title" description="trafficBaskets.description" />
      <div className="w-full space-y-6 px-4 pb-8">
        {!owner && (
          <section className="space-y-3">
            <h2 className="text-base font-semibold">{t('trafficBaskets.myVolumes', { defaultValue: 'My volumes' })}</h2>
            <MyVolumes />
          </section>
        )}
        {owner && (
          <section className="space-y-3">
            <h2 className="text-base font-semibold">{t('trafficBaskets.definitions', { defaultValue: 'Baskets' })}</h2>
            <p className="text-muted-foreground text-sm">
              {t('trafficBaskets.definitionsHint', {
                defaultValue:
                  'A basket is a set of groups. Usage on a node is charged to the basket of the groups it serves: first the admin’s own volume in that basket, then its shared volume. When both are empty, only that basket’s configs stop working for the admin’s users.',
              })}
            </p>
            <BasketDefinitions />
          </section>
        )}
        {canManage && (
          <>
            <Separator />
            <section className="space-y-3">
              <h2 className="text-base font-semibold">{t('trafficBaskets.adminVolumes', { defaultValue: "Admins' volumes" })}</h2>
              <AdminVolumes />
            </section>
          </>
        )}
      </div>
    </div>
  )
}
