// Pengu fork, "Subscription pages": every admin picks the page their own users see in a browser.
// The previews are the real pages rendered with a made-up user. Their scripts come from third parties,
// so each one runs in a sandboxed iframe (an origin of its own, no access to this dashboard's session).
import PageHeader from '@/components/layout/page-header'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Separator } from '@/components/ui/separator'
import { Skeleton } from '@/components/ui/skeleton'
import { useAdmin } from '@/hooks/use-admin'
import { cn } from '@/lib/utils'
import { fetcher } from '@/service/http'
import { isLimited } from '@/utils/rbac'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Copy, Download, ExternalLink, Eye, Loader2, Monitor, Smartphone, Trash2, TriangleAlert, Users } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { joinURL } from 'ufo'

type SubPage = {
  key: string
  kind: 'default' | 'builtin' | 'catalog' | 'custom'
  name: string
  installed: boolean
  author?: string | null
  homepage?: string | null
  license?: string | null
  version?: string | null
  description?: Record<string, string>
  loads?: Record<string, string>
  template?: string | null
  admins?: number | null
}

type SubPages = {
  can_manage: boolean
  install_blocker?: string | null
  selected: string
  selected_template?: string | null
  selected_missing: boolean
  preview_base: string
  pages: SubPage[]
}

type Device = 'mobile' | 'desktop'

const QUERY_KEY = ['sub-pages']
// A preview link lasts an hour; a new one is taken after 50 minutes so an open page never shows a dead frame
const PREVIEW_BASE_LIFETIME = 50 * 60 * 1000
const SANDBOX = 'allow-scripts allow-popups allow-forms allow-modals'
const PHONE_WIDTH = 390

const errorDetail = (error: unknown): string | undefined => {
  const detail = (error as { data?: { detail?: unknown } })?.data?.detail
  return typeof detail === 'string' ? detail : undefined
}

function Thumbnail({ src, title }: { src: string; title: string }) {
  const frame = useRef<HTMLDivElement>(null)
  const [box, setBox] = useState({ width: 0, height: 0 })
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    const element = frame.current
    if (!element) return
    const measure = () => setBox({ width: element.clientWidth, height: element.clientHeight })
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  useEffect(() => setLoaded(false), [src])

  // A phone-wide page, shrunk to about two thirds of the card; the frame is as tall as what is visible
  const scale = box.width ? Math.min(0.72, (box.width * 0.66) / PHONE_WIDTH) : 0.6
  const width = PHONE_WIDTH * scale

  return (
    <div ref={frame} className="relative flex h-full w-full items-start justify-center overflow-hidden" aria-hidden="true">
      {/* Left to right on purpose: the frame is scaled from its top left corner, also in a right-to-left dashboard */}
      <div dir="ltr" style={{ width, height: box.height }} className="bg-background relative overflow-hidden rounded-t-xl border border-b-0 shadow-sm">
        {!loaded && <Skeleton className="absolute inset-0 rounded-none" />}
        {box.height > 0 && (
          <iframe
            src={src}
            title={title}
            sandbox={SANDBOX}
            loading="lazy"
            tabIndex={-1}
            scrolling="no"
            onLoad={() => setLoaded(true)}
            style={{ width: PHONE_WIDTH, height: box.height / scale, transform: `scale(${scale})`, transformOrigin: 'top left' }}
            className="pointer-events-none absolute top-0 left-0 max-w-none border-0"
          />
        )}
      </div>
    </div>
  )
}

export default function SubscriptionPagesPage() {
  const { t, i18n } = useTranslation()
  const client = useQueryClient()
  const { admin } = useAdmin()
  const limited = isLimited(admin)
  const { data, isLoading, isError } = useQuery({
    queryKey: QUERY_KEY,
    queryFn: () => fetcher<SubPages>('/api/sub-pages'),
    retry: false,
    refetchInterval: 20 * 60 * 1000,
  })
  const [busy, setBusy] = useState<string | null>(null)
  const [preview, setPreview] = useState<SubPage | null>(null)
  const [device, setDevice] = useState<Device>('mobile')
  const [removing, setRemoving] = useState<SubPage | null>(null)
  const [previewBase, setPreviewBase] = useState<{ base: string; at: number } | null>(null)

  useEffect(() => {
    if (!data) return
    setPreviewBase(current => (current && Date.now() - current.at < PREVIEW_BASE_LIFETIME ? current : { base: data.preview_base, at: Date.now() }))
  }, [data])

  // No trailing slash, like a real subscription link: pages build their own requests as <path>/info
  const previewUrl = (page: SubPage) => (previewBase ? joinURL(import.meta.env.VITE_BASE_API || '/', previewBase.base, page.key) : '')

  const localized = (texts?: Record<string, string>) => {
    const language = i18n.language?.split('-')[0] ?? 'en'
    return texts?.[language] ?? texts?.en ?? ''
  }
  const pageName = (page: SubPage) => {
    if (page.kind === 'default') return t('subPages.defaultName', { defaultValue: 'Panel default' })
    if (page.kind === 'builtin') return t('subPages.builtinName', { defaultValue: 'PasarGuard built-in' })
    return page.name
  }
  const pageDescription = (page: SubPage) => {
    if (page.kind === 'default') return t('subPages.defaultDescription', { defaultValue: 'The page the panel owner set for everyone. Your users get it until you choose another one.' })
    if (page.kind === 'builtin') return t('subPages.builtinDescription', { defaultValue: 'The simple page that ships with PasarGuard.' })
    if (page.kind === 'custom') return t('subPages.customDescription', { defaultValue: 'A page the panel owner added by hand.' })
    return localized(page.description)
  }

  const run = async (key: string, request: () => Promise<SubPages>, done: string, failed: string) => {
    setBusy(key)
    try {
      client.setQueryData(QUERY_KEY, await request())
      toast.success(done)
    } catch (error) {
      toast.error(failed, { description: errorDetail(error) })
    } finally {
      setBusy(null)
    }
  }

  const choose = (page: SubPage) =>
    run(
      `choose:${page.key}`,
      () => fetcher<SubPages>('/api/sub-pages/selection', { method: 'PUT', body: { key: page.key } }),
      t('subPages.chosen', { defaultValue: 'Your users now get “{{name}}”', name: pageName(page) }),
      t('subPages.chooseFailed', { defaultValue: 'Could not change your subscription page' }),
    )
  const install = (page: SubPage) =>
    run(
      `install:${page.key}`,
      () => fetcher<SubPages>(`/api/sub-pages/${page.key}/install`, { method: 'POST' }),
      t('subPages.installed', { defaultValue: '“{{name}}” was installed', name: pageName(page) }),
      t('subPages.installFailed', { defaultValue: 'Could not install this page' }),
    )
  const remove = (page: SubPage) =>
    run(
      `remove:${page.key}`,
      () => fetcher<SubPages>(`/api/sub-pages/${page.key}`, { method: 'DELETE' }),
      t('subPages.removed', { defaultValue: '“{{name}}” was removed', name: pageName(page) }),
      t('subPages.removeFailed', { defaultValue: 'Could not remove this page' }),
    )

  const copyTemplate = async (template: string) => {
    try {
      await navigator.clipboard.writeText(template)
      toast.success(t('subPages.pathCopied', { defaultValue: 'Path copied' }))
    } catch {
      toast.error(template)
    }
  }

  const card = (page: SubPage) => {
    const selected = data?.selected === page.key
    const name = pageName(page)
    const description = pageDescription(page)
    const loads = data?.can_manage ? localized(page.loads) : ''
    return (
      <div key={page.key} className={cn('bg-card flex min-w-0 flex-col overflow-hidden rounded-lg border transition-colors', selected && 'border-primary ring-primary/30 ring-2')}>
        <div className="bg-muted/40 relative h-72 w-full border-b px-3 pt-3">
          {page.installed && previewBase ? (
            <button
              type="button"
              className="focus-visible:ring-ring block h-full w-full cursor-zoom-in rounded-t-md focus-visible:ring-2 focus-visible:outline-none"
              onClick={() => setPreview(page)}
              aria-label={`${t('subPages.preview', { defaultValue: 'Preview' })}: ${name}`}
            >
              <Thumbnail src={previewUrl(page)} title={name} />
            </button>
          ) : (
            <div className="text-muted-foreground flex h-full flex-col items-center justify-center gap-2 text-center text-sm">
              <Download className="h-6 w-6" />
              <span>{t('subPages.installToPreview', { defaultValue: 'Install it to see the preview' })}</span>
            </div>
          )}
          {selected && (
            <Badge className="absolute end-3 top-3 gap-1">
              <Check className="h-3 w-3" />
              {t('subPages.selected', { defaultValue: 'Selected' })}
            </Badge>
          )}
        </div>
        <div className="flex min-w-0 flex-1 flex-col gap-2 p-3">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <h3 className="truncate text-base font-semibold" dir="auto">
              {name}
            </h3>
            {page.kind === 'custom' && <Badge variant="outline">{t('subPages.customTag', { defaultValue: 'Custom' })}</Badge>}
            {page.version && (
              <Badge variant="secondary" dir="ltr">
                {page.version}
              </Badge>
            )}
          </div>
          {description && <p className="text-muted-foreground text-sm leading-relaxed">{description}</p>}
          {page.homepage && (
            <div className="text-muted-foreground flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs">
              <a href={page.homepage} target="_blank" rel="noopener noreferrer" className="hover:text-foreground inline-flex min-w-0 items-center gap-1 underline-offset-2 hover:underline">
                <ExternalLink className="h-3 w-3 shrink-0" />
                <span className="truncate" dir="ltr">
                  {page.author}
                </span>
              </a>
              <span>{page.license ? t('subPages.license', { defaultValue: 'License: {{license}}', license: page.license }) : t('subPages.noLicense', { defaultValue: 'No license stated' })}</span>
            </div>
          )}
          {loads && (
            <p className="text-muted-foreground text-xs leading-relaxed">
              <span className="text-foreground font-medium">{t('subPages.loads', { defaultValue: 'From other sites:' })}</span> {loads}
            </p>
          )}
          {data?.can_manage && page.installed && (
            <div className="text-muted-foreground flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs">
              {typeof page.admins === 'number' && (
                <span className="inline-flex items-center gap-1">
                  <Users className="h-3 w-3" />
                  {t('subPages.adminsUsing', { defaultValue: '{{count}} admins chose it', count: page.admins })}
                </span>
              )}
              {page.template && (
                <button
                  type="button"
                  className="hover:text-foreground inline-flex min-w-0 items-center gap-1"
                  title={t('subPages.copyPath', { defaultValue: 'Copy the template path (for an admin’s “Subscription template” field)' })}
                  onClick={() => copyTemplate(page.template!)}
                >
                  <Copy className="h-3 w-3 shrink-0" />
                  <code className="truncate" dir="ltr">
                    {page.template}
                  </code>
                </button>
              )}
            </div>
          )}
          <div className="mt-auto flex flex-wrap items-center gap-2 pt-2">
            {page.installed ? (
              <>
                <Button type="button" size="sm" className="gap-1.5" disabled={selected || limited || busy !== null} onClick={() => choose(page)}>
                  {busy === `choose:${page.key}` ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
                  {selected ? t('subPages.selected', { defaultValue: 'Selected' }) : t('subPages.choose', { defaultValue: 'Use this page' })}
                </Button>
                <Button type="button" size="sm" variant="outline" className="gap-1.5" disabled={!previewBase} onClick={() => setPreview(page)}>
                  <Eye className="h-4 w-4" />
                  {t('subPages.preview', { defaultValue: 'Preview' })}
                </Button>
                {data?.can_manage && page.kind === 'catalog' && (
                  <Button
                    type="button"
                    size="icon"
                    variant="ghost"
                    className="text-destructive hover:text-destructive ms-auto h-8 w-8"
                    disabled={busy !== null}
                    aria-label={t('subPages.remove', { defaultValue: 'Remove' })}
                    title={t('subPages.remove', { defaultValue: 'Remove' })}
                    onClick={() => setRemoving(page)}
                  >
                    {busy === `remove:${page.key}` ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
                  </Button>
                )}
              </>
            ) : (
              <Button type="button" size="sm" className="gap-1.5" disabled={busy !== null || !!data?.install_blocker} onClick={() => install(page)}>
                {busy === `install:${page.key}` ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
                {t('subPages.install', { defaultValue: 'Install' })}
              </Button>
            )}
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="flex w-full flex-col items-start gap-2 pb-8">
      <div className="w-full">
        <PageHeader title="subPages.title" description="subPages.description" titleAction={<></>} />
        <Separator />
      </div>
      <div className="w-full space-y-4 p-4">
        {limited && <p className="text-muted-foreground text-sm">{t('subPages.limited', { defaultValue: 'Your account is limited, so the page cannot be changed right now.' })}</p>}
        {data?.selected_missing && (
          <Alert variant="destructive">
            <TriangleAlert className="h-4 w-4" />
            <AlertTitle>{t('subPages.missingTitle', { defaultValue: 'Your chosen page is no longer available' })}</AlertTitle>
            <AlertDescription>
              {t('subPages.missingDescription', { defaultValue: 'Its file was removed from the panel, so your users get the panel’s default page. Choose another page below.' })}
            </AlertDescription>
          </Alert>
        )}
        {data?.selected === 'other' && !data.selected_missing && (
          <Alert>
            <AlertTitle>{t('subPages.otherTitle', { defaultValue: 'The panel owner gave you a page of its own' })}</AlertTitle>
            <AlertDescription>
              {t('subPages.otherDescription', { defaultValue: 'Your users get this template now. Choosing a page below replaces it.' })} <code dir="ltr">{data.selected_template}</code>
            </AlertDescription>
          </Alert>
        )}
        {data?.can_manage && data.install_blocker && (
          <Alert>
            <TriangleAlert className="h-4 w-4" />
            <AlertTitle>{t('subPages.blockerTitle', { defaultValue: 'New pages cannot be installed yet' })}</AlertTitle>
            <AlertDescription>
              {data.install_blocker === 'not_writable'
                ? t('subPages.blockerNotWritable', { defaultValue: 'The panel cannot write into the folder named by CUSTOM_TEMPLATES_DIRECTORY.' })
                : t('subPages.blockerNoDirectory', {
                    defaultValue:
                      'Pages are installed into the custom templates folder. Set CUSTOM_TEMPLATES_DIRECTORY in the panel’s .env file (for example /var/lib/pasarguard/templates/) and restart the panel.',
                  })}
            </AlertDescription>
          </Alert>
        )}
        {isLoading ? (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            {[0, 1, 2].map(index => (
              <Skeleton key={index} className="h-[26rem] w-full rounded-lg" />
            ))}
          </div>
        ) : isError || !data ? (
          <p className="text-muted-foreground py-6 text-center text-sm">{t('subPages.loadFailed', { defaultValue: 'Subscription pages are not available for your account.' })}</p>
        ) : (
          <>
            <div className="grid grid-cols-1 gap-4 min-[2200px]:grid-cols-4 md:grid-cols-2 xl:grid-cols-3">{data.pages.map(card)}</div>
            {data.can_manage && (
              <p className="text-muted-foreground text-xs leading-relaxed">
                {t('subPages.ownerNote', {
                  defaultValue:
                    'Installing downloads the exact reviewed file from the author’s GitHub repository; nothing else is accepted. Every installed page can be chosen by admins whose role has “Subscription pages” on. Your own pages can be added as subscription/pengu-pages/<name>/index.html inside the custom templates folder.',
                })}
              </p>
            )}
          </>
        )}
      </div>

      <Dialog open={preview !== null} onOpenChange={open => !open && setPreview(null)}>
        <DialogContent className="flex h-[92dvh] w-[96vw] max-w-5xl flex-col gap-3 p-4">
          <DialogHeader className="shrink-0">
            <DialogTitle dir="auto">{preview ? pageName(preview) : ''}</DialogTitle>
            <DialogDescription>{t('subPages.previewNote', { defaultValue: 'A sample: the user, usage and servers shown here are made up.' })}</DialogDescription>
          </DialogHeader>
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <Button type="button" size="sm" variant={device === 'mobile' ? 'default' : 'outline'} className="gap-1.5" onClick={() => setDevice('mobile')}>
              <Smartphone className="h-4 w-4" />
              {t('subPages.mobile', { defaultValue: 'Phone' })}
            </Button>
            <Button type="button" size="sm" variant={device === 'desktop' ? 'default' : 'outline'} className="gap-1.5" onClick={() => setDevice('desktop')}>
              <Monitor className="h-4 w-4" />
              {t('subPages.desktop', { defaultValue: 'Desktop' })}
            </Button>
            {preview && (
              <Button asChild type="button" size="sm" variant="ghost" className="ms-auto gap-1.5">
                <a href={previewUrl(preview)} target="_blank" rel="noopener noreferrer">
                  <ExternalLink className="h-4 w-4" />
                  {t('subPages.openInTab', { defaultValue: 'Open in a new tab' })}
                </a>
              </Button>
            )}
          </div>
          <div className="bg-muted/40 flex min-h-0 flex-1 justify-center overflow-hidden rounded-md border">
            {preview && previewBase && (
              <iframe
                key={`${preview.key}:${device}`}
                src={previewUrl(preview)}
                title={pageName(preview)}
                sandbox={SANDBOX}
                className={cn('h-full border-0 bg-white', device === 'mobile' ? 'w-[390px] max-w-full' : 'w-full')}
              />
            )}
          </div>
        </DialogContent>
      </Dialog>

      <AlertDialog open={removing !== null} onOpenChange={open => !open && setRemoving(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('subPages.removeTitle', { defaultValue: 'Remove “{{name}}”?', name: removing ? pageName(removing) : '' })}</AlertDialogTitle>
            <AlertDialogDescription>
              {t('subPages.removeDescription', {
                defaultValue: 'Admins who chose this page go back to the panel’s default page ({{count}} now). You can install it again later.',
                count: removing?.admins ?? 0,
              })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('cancel', { defaultValue: 'Cancel' })}</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                const page = removing
                setRemoving(null)
                if (page) void remove(page)
              }}
            >
              {t('subPages.remove', { defaultValue: 'Remove' })}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
