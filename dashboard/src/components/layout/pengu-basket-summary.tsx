// Pengu fork: an admin's traffic baskets next to its name (volumes are set in the admin dialog, features/admins/components/pengu-admin-baskets.tsx).
import { Progress } from '@/components/ui/progress'
import { fetcher } from '@/service/http'
import { formatBytes } from '@/utils/formatByte'
import { useQuery } from '@tanstack/react-query'
import { Layers } from 'lucide-react'
import { useTranslation } from 'react-i18next'

type Volume = { data_limit: number | null; used_traffic: number; remaining: number | null; blocked: boolean }
type MyBaskets = { basket_mode: boolean; shared: Volume | null; baskets: (Volume & { basket_id: number; name: string })[] }

export function useMyBaskets(enabled = true) {
  return useQuery({
    queryKey: ['traffic-baskets-me'],
    queryFn: () => fetcher<MyBaskets>('/api/pengu/baskets/me'),
    enabled,
    retry: false,
    staleTime: 60_000,
  })
}

export function BasketSummary() {
  const { t } = useTranslation()
  const { data } = useMyBaskets()
  if (!data?.basket_mode) return null
  const rows = [
    ...data.baskets.filter(b => (b.data_limit ?? 0) > 0 || b.used_traffic > 0).map(b => ({ key: `b${b.basket_id}`, name: b.name, v: b as Volume })),
    ...(data.shared && (data.shared.data_limit == null || data.shared.data_limit > 0) ? [{ key: 'shared', name: t('trafficBaskets.sharedShort', { defaultValue: 'Shared' }), v: data.shared }] : []),
  ]
  if (!rows.length) return null
  return (
    <div className="flex flex-col gap-1.5">
      {rows.map(({ key, name, v }) => (
        <div key={key} className="flex flex-col gap-1">
          <div className="flex items-center gap-2">
            <Layers className="size-3" />
            <span className={v.blocked ? 'text-destructive' : undefined}>
              {name}:{' '}
              <span dir="ltr" style={{ unicodeBidi: 'isolate' }}>
                {v.data_limit == null ? '∞' : `${formatBytes(v.remaining ?? 0)} / ${formatBytes(v.data_limit)}`}
              </span>
            </span>
          </div>
          {v.data_limit != null && v.data_limit > 0 && (
            <Progress value={Math.min(100, (v.used_traffic / v.data_limit) * 100)} className="h-1" indicatorClassName={v.blocked ? 'bg-destructive' : undefined} />
          )}
        </div>
      ))}
    </div>
  )
}

/**
 * Always visible at the bottom of the sidebar for a reseller in basket mode: what is left of each
 * volume it has (every basket with a volume, and the overall volume when it has one).
 */
export function BasketRemaining() {
  const { t } = useTranslation()
  const { data } = useMyBaskets()
  if (!data?.basket_mode) return null
  const rows = [
    ...data.baskets.filter(b => (b.data_limit ?? 0) > 0).map(b => ({ key: `b${b.basket_id}`, name: b.name, v: b as Volume })),
    ...(data.shared && (data.shared.data_limit == null || data.shared.data_limit > 0) ? [{ key: 'shared', name: t('trafficBaskets.sharedShort', { defaultValue: 'Overall' }), v: data.shared }] : []),
  ]
  if (!rows.length) return null
  return (
    <div className="bg-sidebar-accent/40 mx-2 mb-1 flex flex-col gap-1.5 rounded-md border px-3 py-2 text-xs">
      <span className="text-muted-foreground flex items-center gap-1.5 font-medium">
        <Layers className="size-3" />
        {t('trafficBaskets.remaining', { defaultValue: 'Remaining volume' })}
      </span>
      {rows.map(({ key, name, v }) => (
        <div key={key} className="flex flex-col gap-1">
          <div className="flex items-center justify-between gap-2">
            <span className={v.blocked ? 'text-destructive' : undefined}>{name}</span>
            <span dir="ltr" className={v.blocked ? 'text-destructive font-semibold' : 'font-semibold'} style={{ unicodeBidi: 'isolate' }}>
              {v.data_limit == null ? '∞' : formatBytes(v.remaining ?? 0)}
            </span>
          </div>
          {v.data_limit != null && v.data_limit > 0 && (
            <Progress value={Math.max(0, Math.min(100, ((v.remaining ?? 0) / v.data_limit) * 100))} className="h-1" indicatorClassName={v.blocked ? 'bg-destructive' : undefined} />
          )}
        </div>
      ))}
    </div>
  )
}
