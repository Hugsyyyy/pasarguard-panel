import PageHeader from '@/components/layout/page-header'
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Separator } from '@/components/ui/separator'
import { Switch } from '@/components/ui/switch'
import { VariablesPopover } from '@/components/ui/variables-popover'
import { useAdmin } from '@/hooks/use-admin'
import { cn } from '@/lib/utils'
import { fetcher } from '@/service/http'
import { DndContext, KeyboardSensor, PointerSensor, closestCenter, useSensor, useSensors, type DragEndEvent } from '@dnd-kit/core'
import { SortableContext, arrayMove, rectSortingStrategy, sortableKeyboardCoordinates, useSortable } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowDown, ArrowUp, Eye, EyeOff, Grip, RotateCcw, Save, Search } from 'lucide-react'
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { isLimited } from '@/utils/rbac'

type HostPreference = { id: number; remark: string; default_remark: string; priority: number; default_priority: number; is_customized: boolean; enabled: boolean }
type ResetConfirmation = 'order' | 'all' | null

function SortableHostRow({
  id,
  disabled,
  label,
  rowRef,
  children,
}: {
  id: number
  disabled: boolean
  label: string
  rowRef: (element: HTMLDivElement | null) => void
  children: (dragHandle: ReactNode) => ReactNode
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id, disabled })
  const dragHandle = (
    <Button type="button" variant="outline" size="icon" className="h-8 w-8 shrink-0 touch-none" aria-label={label} title={label} disabled={disabled} {...attributes} {...listeners}>
      <Grip className="h-4 w-4" />
    </Button>
  )

  return (
    <div
      data-host-row={id}
      ref={element => {
        setNodeRef(element)
        rowRef(element)
      }}
      style={{ transform: CSS.Transform.toString(transform), transition, zIndex: isDragging ? 1 : undefined }}
      className={isDragging ? 'relative opacity-70' : ''}
    >
      {children(dragHandle)}
    </div>
  )
}

export default function HostPreferencesPage() {
  const { t } = useTranslation()
  const client = useQueryClient()
  const { admin } = useAdmin()
  const limited = isLimited(admin)
  const { data, isLoading, isError } = useQuery({ queryKey: ['host-preferences'], queryFn: () => fetcher<HostPreference[]>('/api/hosts/preferences'), retry: false })
  const [hosts, setHosts] = useState<HostPreference[]>([])
  const [saving, setSaving] = useState(false)
  const [search, setSearch] = useState('')
  const [resetConfirmation, setResetConfirmation] = useState<ResetConfirmation>(null)
  const [highlighted, setHighlighted] = useState<number | null>(null)
  const highlightTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const rowRefs = useRef(new Map<number, HTMLDivElement>())
  const previousPositions = useRef<Map<number, DOMRect> | null>(null)
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 6 } }), useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }))

  useEffect(() => {
    if (data) setHosts(data)
  }, [data])
  useEffect(
    () => () => {
      if (highlightTimer.current) clearTimeout(highlightTimer.current)
    },
    [],
  )

  const capturePositions = () => {
    previousPositions.current = new Map(
      hosts.flatMap(host => {
        const row = rowRefs.current.get(host.id)
        return row ? [[host.id, row.getBoundingClientRect()] as const] : []
      }),
    )
  }

  useLayoutEffect(() => {
    const previous = previousPositions.current
    previousPositions.current = null
    if (!previous || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    for (const [hostId, previousRect] of previous) {
      const row = rowRefs.current.get(hostId)
      if (!row) continue
      const offsetY = previousRect.top - row.getBoundingClientRect().top
      if (offsetY === 0) continue
      row.animate([{ transform: `translateY(${offsetY}px)` }, { transform: 'translateY(0)' }], {
        duration: 240,
        easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)',
      })
    }
  }, [hosts])

  const visibleHosts = useMemo(() => {
    const term = search.trim().toLocaleLowerCase()
    return term ? hosts.filter(host => `${host.remark} ${host.default_remark}`.toLocaleLowerCase().includes(term)) : hosts
  }, [hosts, search])
  const movableVisibleHosts = useMemo(() => visibleHosts.filter(host => host.enabled), [visibleHosts])

  const markMoved = (id: number) => {
    setHighlighted(id)
    if (highlightTimer.current) clearTimeout(highlightTimer.current)
    highlightTimer.current = setTimeout(() => setHighlighted(null), 1000)
  }

  const reorder = (fromId: number, toId: number) => {
    const visibleIds = movableVisibleHosts.map(host => host.id)
    const fromIndex = visibleIds.indexOf(fromId)
    const toIndex = visibleIds.indexOf(toId)
    if (fromIndex < 0 || toIndex < 0 || fromIndex === toIndex) return
    const reorderedVisible = arrayMove(movableVisibleHosts, fromIndex, toIndex)
    capturePositions()
    setHosts(current => {
      const visiblePositions = current.flatMap((host, index) => (visibleIds.includes(host.id) ? [index] : []))
      const reordered = [...current]
      visiblePositions.forEach((position, index) => {
        const host = reorderedVisible[index]
        reordered[position] = host
      })
      return reordered.map((host, priority) => ({ ...host, priority }))
    })
    markMoved(fromId)
  }

  const handleDragEnd = (event: DragEndEvent) => {
    if (event.over) reorder(Number(event.active.id), Number(event.over.id))
  }

  const move = (host: HostPreference, delta: number) => {
    const index = movableVisibleHosts.findIndex(item => item.id === host.id)
    const target = movableVisibleHosts[index + delta]
    if (target) reorder(host.id, target.id)
  }

  const onRemarkKeyDown = (event: KeyboardEvent<HTMLInputElement>, host: HostPreference) => {
    if (!event.altKey || (event.key !== 'ArrowUp' && event.key !== 'ArrowDown')) return
    event.preventDefault()
    move(host, event.key === 'ArrowUp' ? -1 : 1)
  }

  const resetOrder = () => {
    capturePositions()
    setHosts(current => {
      const enabledHosts = current.filter(host => host.enabled).sort((a, b) => a.default_priority - b.default_priority || a.id - b.id)
      let enabledIndex = 0
      return current.map(host => (host.enabled ? { ...enabledHosts[enabledIndex++], priority: host.priority } : host))
    })
  }

  const errorMessage = (error: unknown) => {
    const status = (error as { status?: number } | null)?.status
    if (status === 403) return t('hostPreferences.forbidden', { defaultValue: 'Your account cannot change My Hosts right now.' })
    if (status === 422) return t('hostPreferences.invalidRemark', { defaultValue: 'A server name is not valid. Check the variables in braces.' })
    return t('hostPreferences.saveFailed', { defaultValue: 'Could not save host preferences' })
  }

  // A real reset on the server: also forgets hosts this page no longer shows.
  const resetAll = async () => {
    setSaving(true)
    try {
      await fetcher('/api/hosts/preferences', { method: 'DELETE' })
      await client.invalidateQueries({ queryKey: ['host-preferences'] })
      toast.success(t('hostPreferences.resetDone', { defaultValue: 'All host preferences were reset' }))
    } catch (error) {
      toast.error(errorMessage(error))
    } finally {
      setSaving(false)
    }
  }

  const resetName = (host: HostPreference) => {
    setHosts(current => current.map(item => (item.id === host.id ? { ...item, remark: item.default_remark } : item)))
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    requestAnimationFrame(() =>
      rowRefs.current.get(host.id)?.animate(
        [
          { opacity: 0.55, transform: 'scale(0.985)' },
          { opacity: 1, transform: 'scale(1)' },
        ],
        { duration: 260, easing: 'ease-out' },
      ),
    )
  }

  const save = async () => {
    if (hosts.some(host => !host.remark.trim())) {
      toast.error(t('hostPreferences.remarkRequired', { defaultValue: 'A server name cannot be empty' }))
      return
    }
    // Positions are sent only when the order differs from the shared one; otherwise the users keep
    // following the owner's order (and any later change to it).
    const enabledIds = hosts.filter(host => host.enabled).map(host => host.id)
    const defaultIds = hosts
      .filter(host => host.enabled)
      .sort((a, b) => a.default_priority - b.default_priority || a.id - b.id)
      .map(host => host.id)
    const customOrder = enabledIds.some((id, index) => id !== defaultIds[index])
    setSaving(true)
    try {
      await fetcher<HostPreference[]>('/api/hosts/preferences', {
        method: 'PUT',
        body: hosts.map(({ id, remark, default_remark, enabled }, index) => ({
          id,
          remark: remark.trim() === default_remark.trim() ? null : remark,
          priority: customOrder ? index : null,
          enabled,
        })),
      })
      await client.invalidateQueries({ queryKey: ['host-preferences'] })
      toast.success(t('hostPreferences.saved', { defaultValue: 'Host preferences saved' }))
    } catch (error) {
      if ((error as { status?: number } | null)?.status === 409) {
        toast.error(t('hostPreferences.listChanged', { defaultValue: 'The host list has changed. It was reloaded; review it and save again.' }))
        await client.invalidateQueries({ queryKey: ['host-preferences'] })
      } else {
        toast.error(errorMessage(error))
      }
    } finally {
      setSaving(false)
    }
  }

  // Rendered twice: in the header row on desktop, above the host list on mobile.
  const toolbar = (className: string) => (
    <div className={cn('flex w-full flex-col gap-2 lg:flex-row lg:items-center lg:justify-end lg:rtl:flex-row-reverse', className)}>
      {hosts.length > 8 && (
        <div className="relative min-w-0 lg:w-72 xl:w-80">
          <Search className="text-muted-foreground absolute start-3 top-2.5 h-4 w-4" />
          <Input
            value={search}
            onChange={event => setSearch(event.target.value)}
            placeholder={t('hostPreferences.search', { defaultValue: 'Search hosts' })}
            aria-label={t('hostPreferences.search', { defaultValue: 'Search hosts' })}
            className="ps-9"
          />
        </div>
      )}
      <div className="grid grid-cols-2 gap-2 lg:flex lg:shrink-0">
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="w-full gap-2 px-2 text-xs whitespace-nowrap sm:text-sm lg:h-9 lg:w-9 lg:px-0"
          aria-label={t('hostPreferences.resetOrder', { defaultValue: 'Reset order' })}
          title={t('hostPreferences.resetOrder', { defaultValue: 'Reset order' })}
          disabled={limited || isError}
          onClick={() => setResetConfirmation('order')}
        >
          <ArrowUp className="h-4 w-4 shrink-0" />
          <span className="lg:sr-only">{t('hostPreferences.resetOrder', { defaultValue: 'Reset order' })}</span>
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="w-full gap-2 px-2 text-xs whitespace-nowrap sm:text-sm lg:h-9 lg:w-9 lg:px-0"
          aria-label={t('hostPreferences.resetAll', { defaultValue: 'Reset all' })}
          title={t('hostPreferences.resetAll', { defaultValue: 'Reset all' })}
          disabled={saving || limited || isError}
          onClick={() => setResetConfirmation('all')}
        >
          <RotateCcw className="h-4 w-4 shrink-0" />
          <span className="lg:sr-only">{t('hostPreferences.resetAll', { defaultValue: 'Reset all' })}</span>
        </Button>
      </div>
    </div>
  )

  return (
    <div className="flex w-full flex-col items-start gap-2 pb-8">
      <div className="w-full">
        <PageHeader title="hostPreferences.title" description="hostPreferences.description" titleAction={<VariablesPopover includeProtocolTransport side="bottom" align="end" />} />
        <div className="flex items-start gap-2 px-4 pb-3 lg:items-center">
          <p className="text-muted-foreground min-w-0 flex-1 text-sm">
            {t('hostPreferences.subscriptionNote', { defaultValue: 'Names, order, and visible or hidden hosts are reflected in your users’ subscription links and copied configurations.' })}
          </p>
          {toolbar('hidden lg:flex lg:w-auto lg:shrink-0')}
        </div>
        <Separator />
      </div>
      <div className="w-full space-y-3 p-4">
        {toolbar('lg:hidden')}
        {limited && <p className="text-muted-foreground text-sm">{t('hostPreferences.limited', { defaultValue: 'Your account is limited, so changes cannot be saved right now.' })}</p>}
        {isLoading ? (
          <p>{t('loading', { defaultValue: 'Loading...' })}</p>
        ) : isError ? (
          <p className="text-muted-foreground py-6 text-center text-sm">{t('hostPreferences.loadFailed', { defaultValue: 'My Hosts is not available for your account.' })}</p>
        ) : (
          <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
            <SortableContext items={visibleHosts.map(host => host.id)} strategy={rectSortingStrategy}>
              <div className="grid grid-cols-1 gap-2 lg:grid-cols-2 2xl:grid-cols-3">
                {visibleHosts.map(host => {
                  const invalidRemark = !host.remark.trim()
                  // Same rule as saving: a name equal to the original (ignoring outer spaces) is not custom
                  const customized = host.remark.trim() !== host.default_remark.trim()
                  return (
                    <SortableHostRow
                      key={host.id}
                      id={host.id}
                      disabled={!host.enabled}
                      label={t('hostPreferences.dragToReorder', { defaultValue: 'Drag to reorder' })}
                      rowRef={element => {
                        if (element) rowRefs.current.set(host.id, element)
                        else rowRefs.current.delete(host.id)
                      }}
                    >
                      {dragHandle => (
                        <div
                          className={`flex min-w-0 flex-col gap-2 rounded-md border p-2.5 transition-colors sm:p-3 ${highlighted === host.id ? 'bg-accent/70' : ''} ${!host.enabled ? 'opacity-70' : ''}`}
                        >
                          <div className="flex min-w-0 flex-col gap-2">
                            <Input
                              aria-label={`${t('hostPreferences.hostName', { defaultValue: 'Host name' })}: ${host.default_remark}`}
                              value={host.remark}
                              maxLength={256}
                              disabled={!host.enabled}
                              placeholder={host.default_remark}
                              // Same direction rule as the original name below (and most client apps): from the first letter
                              dir="auto"
                              isError={invalidRemark}
                              aria-invalid={invalidRemark}
                              onKeyDown={event => onRemarkKeyDown(event, host)}
                              style={{ fontFamily: 'system-ui, "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", sans-serif' }}
                              onChange={event => setHosts(current => current.map(item => (item.id === host.id ? { ...item, remark: event.target.value } : item)))}
                            />
                            {/* Always show which shared host this card is, renamed or not */}
                            <div className="text-muted-foreground flex min-w-0 items-center gap-1.5 text-xs" title={host.default_remark}>
                              <span className="shrink-0">{t('hostPreferences.originalLabel', { defaultValue: 'Original name' })}:</span>
                              <bdi dir="auto" className="min-w-0 truncate">
                                {host.default_remark}
                              </bdi>
                              {customized && (
                                <span className="bg-primary/10 text-primary shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium">{t('hostPreferences.custom', { defaultValue: 'Custom' })}</span>
                              )}
                            </div>
                            {invalidRemark && <span className="text-destructive text-xs">{t('hostPreferences.remarkRequired', { defaultValue: 'A server name cannot be empty' })}</span>}
                          </div>
                          <div className="flex min-w-0 flex-wrap items-center justify-between gap-x-2 gap-y-2 border-t pt-2">
                            <div className="flex shrink-0 items-center gap-1">
                              {dragHandle}
                              <Button
                                type="button"
                                variant="outline"
                                size="icon"
                                className="h-8 w-8 lg:hidden"
                                aria-label={t('hostPreferences.moveUp', { defaultValue: 'Move up' })}
                                title={t('hostPreferences.moveUp', { defaultValue: 'Move up' })}
                                disabled={!host.enabled || movableVisibleHosts.findIndex(item => item.id === host.id) === 0}
                                onClick={() => move(host, -1)}
                              >
                                <ArrowUp className="h-4 w-4" />
                              </Button>
                              <Button
                                type="button"
                                variant="outline"
                                size="icon"
                                className="h-8 w-8 lg:hidden"
                                aria-label={t('hostPreferences.moveDown', { defaultValue: 'Move down' })}
                                title={t('hostPreferences.moveDown', { defaultValue: 'Move down' })}
                                disabled={!host.enabled || movableVisibleHosts.findIndex(item => item.id === host.id) === movableVisibleHosts.length - 1}
                                onClick={() => move(host, 1)}
                              >
                                <ArrowDown className="h-4 w-4" />
                              </Button>
                            </div>
                            <div className="flex min-w-0 flex-wrap items-center justify-end gap-x-2 gap-y-1">
                              {/* Say what the switch does: show this host to the reseller's users, or hide it */}
                              <label
                                className={cn('flex cursor-pointer items-center gap-1.5 text-xs font-medium select-none', host.enabled ? 'text-foreground' : 'text-muted-foreground')}
                                title={t('hostPreferences.visibilityHint', { defaultValue: "When off, this server is left out of your users' subscriptions." })}
                              >
                                {host.enabled ? <Eye className="h-3.5 w-3.5 shrink-0" /> : <EyeOff className="h-3.5 w-3.5 shrink-0" />}
                                <span className="whitespace-nowrap">
                                  {host.enabled ? t('hostPreferences.shown', { defaultValue: 'Visible' }) : t('hostPreferences.hidden', { defaultValue: 'Hidden' })}
                                </span>
                                <Switch
                                  checked={host.enabled}
                                  onCheckedChange={enabled => setHosts(current => current.map(item => (item.id === host.id ? { ...item, enabled } : item)))}
                                  aria-label={t('hostPreferences.enabled', { defaultValue: 'Show to my users' })}
                                />
                              </label>
                              {customized && (
                                <Button
                                  type="button"
                                  variant="outline"
                                  size="icon"
                                  className="h-8 w-8"
                                  disabled={!host.enabled}
                                  aria-label={t('hostPreferences.resetName', { defaultValue: 'Reset name' })}
                                  title={t('hostPreferences.resetName', { defaultValue: 'Reset name' })}
                                  onClick={() => resetName(host)}
                                >
                                  <RotateCcw className="h-4 w-4" />
                                </Button>
                              )}
                            </div>
                          </div>
                        </div>
                      )}
                    </SortableHostRow>
                  )
                })}
                {!visibleHosts.length && (
                  <p className="text-muted-foreground col-span-full py-6 text-center text-sm">{t('hostPreferences.noResults', { defaultValue: 'No hosts match your search' })}</p>
                )}
              </div>
            </SortableContext>
          </DndContext>
        )}
        <Button onClick={save} disabled={saving || isLoading || isError || limited || hosts.some(host => !host.remark.trim())}>
          <Save className="me-2 h-4 w-4" />
          {t('hostPreferences.save', { defaultValue: 'Save changes' })}
        </Button>
      </div>
      <AlertDialog open={resetConfirmation !== null} onOpenChange={open => !open && setResetConfirmation(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {resetConfirmation === 'all'
                ? t('hostPreferences.resetAllTitle', { defaultValue: 'Reset all host preferences?' })
                : t('hostPreferences.resetOrderTitle', { defaultValue: 'Reset host order?' })}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {resetConfirmation === 'all'
                ? t('hostPreferences.resetAllConfirm', { defaultValue: 'Reset all custom host names, order, and hidden hosts?' })
                : t('hostPreferences.resetOrderConfirm', { defaultValue: 'Restore the default host order? Custom names and hidden hosts will stay unchanged.' })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('hostPreferences.cancel', { defaultValue: 'Cancel' })}</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (resetConfirmation === 'all') void resetAll()
                else if (resetConfirmation === 'order') resetOrder()
                setResetConfirmation(null)
              }}
            >
              {t('hostPreferences.confirm', { defaultValue: 'OK' })}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
